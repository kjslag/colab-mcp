# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
from collections.abc import AsyncIterator
import contextlib
from contextlib import AsyncExitStack
from fastmcp import FastMCP, Client
from fastmcp.client.transports import ClientTransport
from fastmcp.dependencies import CurrentContext
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
from fastmcp.server.proxy import FastMCPProxy
from fastmcp.tools.tool import Tool, ToolResult
from mcp.client.session import ClientSession
from mcp.types import TextContent
from typing import Annotated, Any
from urllib.parse import quote, urlsplit, urlunsplit
import webbrowser

from colab_mcp.websocket_server import (
    ColabWebSocketServer,
    COLAB,
    COLAB_ALT_DOMAIN,
    SCRATCH_PATH,
)

UI_CONNECTION_TIMEOUT = 60.0  # secs

FE_CONNECTED_KEY = "fe_connected"
PROXY_TOKEN_KEY = "proxy_token"
PROXY_PORT_KEY = "proxy_port"
INJECTED_TOOL_NAME = "open_colab_browser_connection"
LIST_NOTEBOOK_TOOLS_NAME = "list_notebook_tools"
CALL_NOTEBOOK_TOOL_NAME = "call_notebook_tool"

NOT_CONNECTED_MSG = (
    "No Colab tab is connected. Call open_colab_browser_connection first and "
    "wait for it to return true."
)
ALREADY_CONNECTED_MSG = (
    "A Colab tab is already connected, and this server cannot tell which "
    "notebook it shows. To keep working in that tab, use the notebook tools "
    "directly (get_cells shows its contents). To switch notebooks, ask the "
    "user to close that tab, then call this tool again with notebook_url."
)

# The URL fragment carries the credential for this server's WebSocket, so it
# is only ever attached to pages on these hosts.
ALLOWED_NOTEBOOK_HOSTS = frozenset(
    urlsplit(origin).netloc for origin in (COLAB, COLAB_ALT_DOMAIN)
)


def build_connection_url(token: str, port: int, notebook_url: str | None = None) -> str:
    """Returns the Colab URL to open, with the proxy token and port in its fragment.

    Without notebook_url this is the scratch notebook. With it, the URL must be
    https on a Colab host, with no userinfo or port. Its path and query are
    percent-encoded, and any fragment it has (such as #scrollTo=...) is dropped.
    """
    base = f"{COLAB}{SCRATCH_PATH}"
    if notebook_url:
        parts = urlsplit(notebook_url.strip())
        host = parts.netloc.lower()
        if parts.scheme != "https" or host not in ALLOWED_NOTEBOOK_HOSTS:
            raise ValueError(
                "notebook_url must be an https URL on "
                f"{' or '.join(sorted(ALLOWED_NOTEBOOK_HOSTS))}, got: {notebook_url!r}"
            )
        path = quote(parts.path, safe="/%-._~!$&'()*+,;=:@")
        query = quote(parts.query, safe="/?%-._~!$&'()*+,;=:@")
        base = urlunsplit(("https", host, path, query, ""))
    return f"{base}#mcpProxyToken={token}&mcpProxyPort={port}"


class ColabTransport(ClientTransport):
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss

    @contextlib.asynccontextmanager
    async def connect_session(self, **session_kwargs) -> AsyncIterator[ClientSession]:
        async with ClientSession(
            self.wss.read_stream, self.wss.write_stream, **session_kwargs
        ) as session:
            yield session

    def __repr__(self) -> str:
        return "<ColabSessionProxyTransport>"


class ColabProxyClient:
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss
        self.stubbed_mcp_client = Client(FastMCP())
        self.proxy_mcp_client: Client | None = None
        self._exit_stack = AsyncExitStack()
        self._start_task = None

    def is_connected(self):
        return self.wss.connection_live.is_set() and self.proxy_mcp_client is not None

    async def await_proxy_connection(self):
        with contextlib.suppress(asyncio.TimeoutError):
            # wait for the connection to be live and for the proxy client to fully initialize
            connection_tasks = asyncio.gather(
                self.wss.connection_live.wait(), self._start_task
            )
            await asyncio.wait_for(
                connection_tasks,
                timeout=UI_CONNECTION_TIMEOUT,
            )

    def client_factory(self):
        if self.is_connected():
            return self.proxy_mcp_client
        # return a client mapped to a stubbed mcp server if there is no session proxy
        return self.stubbed_mcp_client

    async def _start_proxy_client(self):
        # blocks until a websocket connection is made successfully
        self.proxy_mcp_client = await self._exit_stack.enter_async_context(
            Client(ColabTransport(self.wss))
        )

    async def __aenter__(self):
        self._start_task = asyncio.create_task(self._start_proxy_client())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._start_task:
            self._start_task.cancel()
        await self._exit_stack.aclose()


class ColabProxyMiddleware(Middleware):
    def __init__(self, proxy_client: ColabProxyClient):
        self.proxy_client = proxy_client
        self.last_message_connected = self.proxy_client.is_connected()

    async def on_message(self, context: MiddlewareContext, call_next):
        """
        Check for a change to Colab session connectivity on any communication with this MCP server and
        notify the client when the connectivity status has changed.
        """
        context.fastmcp_context.set_state(
            FE_CONNECTED_KEY, self.proxy_client.is_connected()
        )
        context.fastmcp_context.set_state(PROXY_TOKEN_KEY, self.proxy_client.wss.token)
        context.fastmcp_context.set_state(PROXY_PORT_KEY, self.proxy_client.wss.port)

        result = await call_next(context)

        connected = self.proxy_client.is_connected()
        connection_state_changed = connected != self.last_message_connected
        self.last_message_connected = connected
        if connection_state_changed:
            await context.fastmcp_context.send_tool_list_changed()

        return result

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if context.message.name != INJECTED_TOOL_NAME:
            return result
        if self.proxy_client.is_connected():
            return result
        # if the tool call was for open_colab_browser_connection and there is no existing connection, try to await full connection
        await context.fastmcp_context.report_progress(
            progress=1, total=3, message="The user is not connected to the Colab UI"
        )
        await context.fastmcp_context.report_progress(
            progress=2,
            total=3,
            message="Waiting for user to connect in Colab - will wait for 60s",
        )
        await self.proxy_client.await_proxy_connection()
        if self.proxy_client.is_connected():
            await context.fastmcp_context.report_progress(
                progress=3, total=3, message="The Colab UI is successfully connected!"
            )
            return ToolResult(
                content=[TextContent(type="text", text="true")],
                structured_content={"result": True},
            )
        else:
            await context.fastmcp_context.report_progress(
                progress=3,
                total=3,
                message="Timeout while waiting for the user to connect.",
            )
            return ToolResult(
                content=[TextContent(type="text", text="false")],
                structured_content={"result": False},
            )


async def check_session_proxy_tool_fn(
    notebook_url: Annotated[
        str | None,
        "https URL of an existing Colab notebook to open and connect to, for "
        "example https://colab.research.google.com/drive/<id>. Omit to open a "
        "new scratch notebook.",
    ] = None,
    ctx: Context = CurrentContext(),
) -> bool:
    fe_connected = ctx.get_state(FE_CONNECTED_KEY)
    token = ctx.get_state(PROXY_TOKEN_KEY)
    port = ctx.get_state(PROXY_PORT_KEY)
    if fe_connected:
        if notebook_url:
            # Returning true here would let the caller edit whichever notebook
            # is connected while believing it is the one it just asked for.
            raise ToolError(ALREADY_CONNECTED_MSG)
        return True
    try:
        url = build_connection_url(token, port, notebook_url)
    except ValueError as e:
        raise ToolError(str(e)) from e
    webbrowser.open_new(url)
    return False


check_session_proxy_tool = Tool.from_function(
    fn=check_session_proxy_tool_fn,
    name=INJECTED_TOOL_NAME,
    description=(
        "Opens a Google Colab notebook in the user's browser and connects to it, "
        "which unlocks the notebook editing tools. Pass notebook_url to connect "
        "to an existing notebook; omit it for a new scratch notebook. The user "
        "has to click Connect in the tab that opens, and this call waits up to "
        "60 seconds for that. Returns true once a tab is connected and false on "
        "timeout. If a tab is already connected, returns true without opening "
        "anything."
    ),
)


def _connected_client(proxy_client: ColabProxyClient) -> Client:
    if not proxy_client.is_connected():
        raise ToolError(NOT_CONNECTED_MSG)
    return proxy_client.proxy_mcp_client


def make_passthrough_tools(proxy_client: ColabProxyClient) -> list[Tool]:
    """Builds two tools that reach the Colab tab's notebook tools by name.

    The notebook tools are served by the Colab page, so they join this server's
    tool list only once a tab connects, and a client learns about them through
    notifications/tools/list_changed. The two tools built here are listed from
    startup and take the notebook tool's name as an argument, so a client that
    does not act on that notification can still use the notebook. Nothing about
    the notebook tools is hardcoded: their names and schemas are read from the
    connected tab each time.
    """

    async def list_notebook_tools() -> list[dict[str, Any]]:
        client = _connected_client(proxy_client)
        async with client:
            tools = await client.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema,
            }
            for tool in tools
        ]

    async def call_notebook_tool(
        name: Annotated[str, "Name of a tool returned by list_notebook_tools."],
        arguments: Annotated[
            dict[str, Any] | None,
            "Arguments for that tool, matching its inputSchema.",
        ] = None,
    ) -> ToolResult:
        client = _connected_client(proxy_client)
        async with client:
            result = await client.call_tool_mcp(name=name, arguments=arguments or {})
        if result.isError:
            texts = [c.text for c in result.content if isinstance(c, TextContent)]
            raise ToolError("\n".join(texts) or f"Notebook tool {name!r} failed.")
        return ToolResult(
            content=result.content,
            structured_content=result.structuredContent,
        )

    return [
        Tool.from_function(
            fn=list_notebook_tools,
            name=LIST_NOTEBOOK_TOOLS_NAME,
            description=(
                "Lists the notebook tools offered by the connected Colab tab, "
                "with their input schemas. Use this with call_notebook_tool "
                "when the notebook tools (such as get_cells or add_code_cell) "
                "are not in your own tool list after connecting. Requires a "
                "connected tab."
            ),
        ),
        Tool.from_function(
            fn=call_notebook_tool,
            name=CALL_NOTEBOOK_TOOL_NAME,
            description=(
                "Calls one of the connected Colab tab's notebook tools by name "
                "and returns its result. Get the names and argument schemas "
                "from list_notebook_tools. Requires a connected tab."
            ),
        ),
    ]


class ColabSessionProxy:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.proxy_server: FastMCPProxy | None = None
        # list order matters, see: https://gofastmcp.com/servers/middleware#multiple-middleware
        self.middleware: list[Middleware] = []
        self.wss: ColabWebSocketServer | None = None

    async def start_proxy_server(self):
        self.wss = await self._exit_stack.enter_async_context(ColabWebSocketServer())
        proxy_client = await self._exit_stack.enter_async_context(
            ColabProxyClient(self.wss)
        )
        self.proxy_server = FastMCPProxy(
            client_factory=proxy_client.client_factory,
            instructions="Connects to a user's Google Colab session in a browser and allows for interactions with their Google Colab notebook",
        )
        # ColabProxyMiddleware must be first because it sets the fe_connected state
        self.middleware.append(ColabProxyMiddleware(proxy_client))
        self.middleware.append(
            ToolInjectionMiddleware(
                tools=[
                    check_session_proxy_tool,
                    *make_passthrough_tools(proxy_client),
                ]
            )
        )

    async def cleanup(self):
        await self._exit_stack.aclose()
