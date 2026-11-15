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

"""Runs the real server stack against a stand-in for the Colab tab.

Nothing here talks to Colab. FakeColabTab plays the part of the Colab page: it
dials the bridge's WebSocket with the token and port from the URL the bridge
asked the browser to open, then answers MCP requests the way a tab would.
"""

import asyncio
from colab_mcp import session
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
import json
import pytest
import pytest_asyncio
from urllib.parse import parse_qs, urlsplit
import webbrowser
import websockets

NOTEBOOK_URL = "https://colab.research.google.com/drive/abc123"
INJECTED = {
    session.INJECTED_TOOL_NAME,
    session.LIST_NOTEBOOK_TOOLS_NAME,
    session.CALL_NOTEBOOK_TOOL_NAME,
}
TAB_TOOLS = [
    {
        "name": "get_cells",
        "description": "Returns the notebook's cells.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_code_cell",
        "description": "Adds a code cell.",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
]


class FakeColabTab:
    def __init__(self, opened_url: str):
        self.opened_url = opened_url
        fragment = parse_qs(urlsplit(opened_url).fragment)
        self.token = fragment["mcpProxyToken"][0]
        self.port = int(fragment["mcpProxyPort"][0])
        self.calls: list[dict] = []
        self.methods: list[str] = []
        self._ws = None
        self._task = asyncio.create_task(self._serve())

    async def _serve(self):
        self._ws = await websockets.connect(
            f"ws://127.0.0.1:{self.port}?access_token={self.token}",
            origin="https://colab.research.google.com",
            subprotocols=["mcp"],
        )
        async for raw in self._ws:
            msg = json.loads(raw)
            self.methods.append(msg.get("method"))
            if "id" not in msg:
                continue  # a notification
            reply = {"jsonrpc": "2.0", "id": msg["id"]}
            match msg["method"]:
                case "initialize":
                    reply["result"] = {
                        "protocolVersion": msg["params"]["protocolVersion"],
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "fake-colab-tab", "version": "0"},
                    }
                case "tools/list":
                    reply["result"] = {"tools": TAB_TOOLS}
                case "tools/call":
                    params = msg["params"]
                    self.calls.append(params)
                    if params["name"] == "add_code_cell":
                        reply["result"] = {
                            "content": [{"type": "text", "text": "added c1"}]
                        }
                    elif params["name"] == "get_cells":
                        reply["result"] = {"content": [{"type": "text", "text": "[]"}]}
                    else:
                        reply["result"] = {
                            "content": [{"type": "text", "text": "unknown tool"}],
                            "isError": True,
                        }
                case "ping":
                    reply["result"] = {}
                case _:
                    reply["error"] = {"code": -32601, "message": "Method not found"}
            await self._ws.send(json.dumps(reply))

    async def close(self):
        """Closes the socket, as closing the browser tab would."""
        if self._ws is not None:
            await self._ws.close()
        await asyncio.wait_for(self._task, timeout=5)


@pytest.fixture
def tabs(monkeypatch):
    """Every URL the bridge asks the browser to open becomes a FakeColabTab."""
    opened: list[FakeColabTab] = []
    monkeypatch.setattr(
        webbrowser, "open_new", lambda url: opened.append(FakeColabTab(url))
    )
    return opened


@pytest_asyncio.fixture
async def bridge():
    """The server as colab_mcp.main_async wires it, minus the stdio transport."""
    server = FastMCP(name="ColabMCP")
    proxy = session.ColabSessionProxy()
    await proxy.start_proxy_server()
    server.mount(proxy.proxy_server)
    for middleware in proxy.middleware:
        server.add_middleware(middleware)
    try:
        yield server
    finally:
        await proxy.cleanup()


async def tool_names(client: Client) -> set[str]:
    return {tool.name for tool in await client.list_tools()}


async def wait_until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_notebook_tools_are_reachable_before_and_after_connecting(bridge, tabs):
    list_changed = asyncio.Event()

    async def on_message(message):
        root = getattr(message, "root", None)
        if getattr(root, "method", None) == "notifications/tools/list_changed":
            list_changed.set()

    async with Client(bridge, message_handler=on_message) as client:
        # Before any tab connects, the passthrough tools are already listed.
        assert await tool_names(client) == INJECTED
        with pytest.raises(ToolError, match="No Colab tab is connected"):
            await client.call_tool(session.LIST_NOTEBOOK_TOOLS_NAME)
        with pytest.raises(ToolError, match="No Colab tab is connected"):
            await client.call_tool(
                session.CALL_NOTEBOOK_TOOL_NAME, {"name": "get_cells"}
            )

        # Connecting to an existing notebook opens that notebook, not scratch.
        result = await client.call_tool(
            session.INJECTED_TOOL_NAME, {"notebook_url": NOTEBOOK_URL}
        )
        assert result.structured_content == {"result": True}
        assert len(tabs) == 1
        assert tabs[0].opened_url.startswith(f"{NOTEBOOK_URL}#mcpProxyToken=")

        # A client that acts on list_changed now sees the tab's tools directly.
        await asyncio.wait_for(list_changed.wait(), timeout=5)
        assert await tool_names(client) == INJECTED | {"get_cells", "add_code_cell"}
        result = await client.call_tool("add_code_cell", {"code": "1+1"})
        assert result.content[0].text == "added c1"

        # A client that ignores list_changed uses the passthrough tools instead.
        result = await client.call_tool(session.LIST_NOTEBOOK_TOOLS_NAME)
        assert result.structured_content == {"result": TAB_TOOLS}
        result = await client.call_tool(
            session.CALL_NOTEBOOK_TOOL_NAME,
            {"name": "add_code_cell", "arguments": {"code": "2+2"}},
        )
        assert result.content[0].text == "added c1"
        with pytest.raises(ToolError, match="unknown tool"):
            await client.call_tool(
                session.CALL_NOTEBOOK_TOOL_NAME, {"name": "no_such_tool"}
            )
        assert [(c["name"], c.get("arguments")) for c in tabs[0].calls] == [
            ("add_code_cell", {"code": "1+1"}),
            ("add_code_cell", {"code": "2+2"}),
            ("no_such_tool", {}),
        ]

        # Already connected: a plain call is a no-op, but asking for a notebook
        # by URL is refused rather than silently keeping the connected one.
        result = await client.call_tool(session.INJECTED_TOOL_NAME)
        assert result.structured_content == {"result": True}
        with pytest.raises(ToolError, match="already connected"):
            await client.call_tool(
                session.INJECTED_TOOL_NAME,
                {"notebook_url": "https://colab.research.google.com/drive/other"},
            )
        assert len(tabs) == 1

        await tabs[0].close()


@pytest.mark.asyncio
async def test_default_is_the_scratch_notebook(bridge, tabs):
    async with Client(bridge) as client:
        result = await client.call_tool(session.INJECTED_TOOL_NAME)
        assert result.structured_content == {"result": True}
        assert tabs[0].opened_url.startswith(
            "https://colab.research.google.com/notebooks/empty.ipynb#mcpProxyToken="
        )
        await tabs[0].close()


@pytest.mark.asyncio
async def test_rejected_notebook_url_opens_nothing(bridge, tabs):
    async with Client(bridge) as client:
        with pytest.raises(ToolError, match="notebook_url must be an https URL"):
            await client.call_tool(
                session.INJECTED_TOOL_NAME,
                {"notebook_url": "https://example.com/drive/abc123"},
            )
        assert tabs == []


@pytest.mark.asyncio
async def test_switching_notebooks_after_the_tab_closes(bridge, tabs):
    async with Client(bridge) as client:
        await client.call_tool(
            session.INJECTED_TOOL_NAME, {"notebook_url": NOTEBOOK_URL}
        )
        await tabs[0].close()

        async def disconnected():
            return await tool_names(client) == INJECTED

        await wait_until(disconnected)
        with pytest.raises(ToolError, match="No Colab tab is connected"):
            await client.call_tool(
                session.CALL_NOTEBOOK_TOOL_NAME, {"name": "get_cells"}
            )

        other = "https://colab.research.google.com/drive/other"
        result = await client.call_tool(
            session.INJECTED_TOOL_NAME, {"notebook_url": other}
        )
        assert result.structured_content == {"result": True}
        assert tabs[1].opened_url.startswith(f"{other}#mcpProxyToken=")
        result = await client.call_tool(
            session.CALL_NOTEBOOK_TOOL_NAME, {"name": "get_cells"}
        )
        assert result.content[0].text == "[]"
        assert [(c["name"], c.get("arguments")) for c in tabs[1].calls] == [
            ("get_cells", {})
        ]
        # The bridge keeps its MCP session across tabs, so the second tab is
        # not sent a fresh initialize. Whether the real Colab page accepts
        # that is not something this test can show.
        assert "initialize" in tabs[0].methods
        assert "initialize" not in tabs[1].methods

        await tabs[1].close()
