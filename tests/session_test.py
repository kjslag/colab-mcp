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
from colab_mcp import session
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from mcp.types import CallToolResult, TextContent, Tool as McpTool
import pytest
from unittest.mock import patch, AsyncMock, MagicMock, Mock


@pytest.fixture(autouse=True)
def mock_webbrowser(monkeypatch):
    import webbrowser

    mock_open = Mock()
    monkeypatch.setattr(webbrowser, "open_new", mock_open)
    return mock_open


@pytest.fixture
def mock_wss():
    """Provides a mock ColabWebSocketServer instance."""
    return MockColabWebSocketServer()


class MockColabWebSocketServer:
    def __init__(self):
        self.connection_live = asyncio.Event()
        self.read_stream = AsyncMock()
        self.write_stream = AsyncMock()
        self.token = "test-token"
        self.port = 1234

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_proxy_client(mock_wss):
    client = Mock(spec=session.ColabProxyClient)
    client.wss = mock_wss
    client.is_connected.return_value = False
    return client


class TestColabProxyMiddleware:
    @pytest.mark.asyncio
    async def test_connection_live(self, mock_proxy_client):
        """Tests connection state change from disconnected to connected."""
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = True
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", True)
        context.fastmcp_context.set_state.assert_any_call("proxy_token", "test-token")
        context.fastmcp_context.set_state.assert_any_call("proxy_port", 1234)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_not_live(self, mock_proxy_client):
        """Tests connection state change from connected to disconnected."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = False
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", False)
        assert middleware.last_message_connected is False
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_connection_change(self, mock_proxy_client):
        """Tests no connection state change."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", True)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_call_tool_await_connection(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.side_effect = [False, True]
        mock_proxy_client.await_proxy_connection = AsyncMock()
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        context.fastmcp_context.report_progress.assert_called()
        assert result.structured_content == {"result": True}

    @pytest.mark.asyncio
    async def test_on_call_tool_timeout(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.return_value = False
        mock_proxy_client.await_proxy_connection = AsyncMock()
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        assert result.structured_content == {"result": False}


class TestCheckSessionProxyToolFn:
    @pytest.mark.asyncio
    async def test_connected(self):
        ctx = Mock()
        ctx.get_state.side_effect = (
            lambda k: True if k == session.FE_CONNECTED_KEY else None
        )
        assert await session.check_session_proxy_tool_fn(ctx=ctx) is True

    @pytest.mark.asyncio
    async def test_disconnected(self, mock_webbrowser):
        ctx = Mock()

        def get_state(k):
            if k == session.FE_CONNECTED_KEY:
                return False
            if k == session.PROXY_TOKEN_KEY:
                return "test-token"
            if k == session.PROXY_PORT_KEY:
                return 1234
            return None

        ctx.get_state.side_effect = get_state
        assert await session.check_session_proxy_tool_fn(ctx=ctx) is False
        mock_webbrowser.assert_called_once()
        args, _ = mock_webbrowser.call_args
        assert "mcpProxyToken=test-token" in args[0]
        assert "mcpProxyPort=1234" in args[0]
        assert args[0].startswith(
            "https://colab.research.google.com/notebooks/empty.ipynb#"
        )

    @staticmethod
    def _ctx(connected):
        ctx = Mock()
        state = {
            session.FE_CONNECTED_KEY: connected,
            session.PROXY_TOKEN_KEY: "test-token",
            session.PROXY_PORT_KEY: 1234,
        }
        ctx.get_state.side_effect = state.get
        return ctx

    @pytest.mark.asyncio
    async def test_disconnected_opens_notebook_url(self, mock_webbrowser):
        ctx = self._ctx(connected=False)
        url = "https://colab.research.google.com/drive/abc123?authuser=1#scrollTo=x"
        assert (
            await session.check_session_proxy_tool_fn(notebook_url=url, ctx=ctx)
            is False
        )
        mock_webbrowser.assert_called_once_with(
            "https://colab.research.google.com/drive/abc123?authuser=1"
            "#mcpProxyToken=test-token&mcpProxyPort=1234"
        )

    @pytest.mark.asyncio
    async def test_bad_notebook_url_is_rejected(self, mock_webbrowser):
        ctx = self._ctx(connected=False)
        with pytest.raises(ToolError, match="notebook_url must be an https URL"):
            await session.check_session_proxy_tool_fn(
                notebook_url="https://example.com/drive/abc123", ctx=ctx
            )
        mock_webbrowser.assert_not_called()

    @pytest.mark.asyncio
    async def test_connected_with_notebook_url_is_an_error(self, mock_webbrowser):
        ctx = self._ctx(connected=True)
        with pytest.raises(ToolError, match="already connected"):
            await session.check_session_proxy_tool_fn(
                notebook_url="https://colab.research.google.com/drive/abc123",
                ctx=ctx,
            )
        mock_webbrowser.assert_not_called()


class TestBuildConnectionUrl:
    def test_scratch_by_default(self):
        assert session.build_connection_url("tok", 99) == (
            "https://colab.research.google.com/notebooks/empty.ipynb"
            "#mcpProxyToken=tok&mcpProxyPort=99"
        )

    @pytest.mark.parametrize(
        "notebook_url, expected_base",
        [
            (
                "https://colab.research.google.com/drive/abc123",
                "https://colab.research.google.com/drive/abc123",
            ),
            (
                "  https://Colab.Research.Google.com/drive/abc123  ",
                "https://colab.research.google.com/drive/abc123",
            ),
            (
                "https://colab.google.com/drive/abc123",
                "https://colab.google.com/drive/abc123",
            ),
            (
                "https://colab.research.google.com/github/o/r/blob/main/a%20b.ipynb",
                "https://colab.research.google.com/github/o/r/blob/main/a%20b.ipynb",
            ),
            (
                "https://colab.research.google.com/drive/abc123?authuser=1&usp=sharing",
                "https://colab.research.google.com/drive/abc123?authuser=1&usp=sharing",
            ),
            (
                # an existing fragment is replaced, including a stale token
                "https://colab.research.google.com/drive/abc123#mcpProxyToken=old&mcpProxyPort=1",
                "https://colab.research.google.com/drive/abc123",
            ),
            (
                # characters that could break out of a quoted string are encoded
                'https://colab.research.google.com/drive/a"b\\c d',
                "https://colab.research.google.com/drive/a%22b%5Cc%20d",
            ),
        ],
    )
    def test_existing_notebook(self, notebook_url, expected_base):
        assert session.build_connection_url("tok", 99, notebook_url) == (
            f"{expected_base}#mcpProxyToken=tok&mcpProxyPort=99"
        )

    @pytest.mark.parametrize(
        "notebook_url",
        [
            "http://colab.research.google.com/drive/abc123",
            "https://example.com/drive/abc123",
            "https://colab.research.google.com.example.com/drive/abc123",
            "https://colab.research.google.com@example.com/drive/abc123",
            "https://user@colab.research.google.com/drive/abc123",
            "https://colab.research.google.com:8443/drive/abc123",
            "https://example.com/#https://colab.research.google.com/drive/abc123",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "colab.research.google.com/drive/abc123",
        ],
    )
    def test_rejects_urls_off_colab(self, notebook_url):
        with pytest.raises(ValueError, match="notebook_url must be an https URL"):
            session.build_connection_url("tok", 99, notebook_url)


class TestPassthroughTools:
    @staticmethod
    def _tools(proxy_client):
        return {t.name: t for t in session.make_passthrough_tools(proxy_client)}

    @staticmethod
    def _connected_proxy_client(mock_proxy_client):
        mock_proxy_client.is_connected.return_value = True
        mock_proxy_client.proxy_mcp_client = MagicMock()
        mock_proxy_client.proxy_mcp_client.__aenter__ = AsyncMock()
        mock_proxy_client.proxy_mcp_client.__aexit__ = AsyncMock(return_value=False)
        return mock_proxy_client.proxy_mcp_client

    def test_names(self, mock_proxy_client):
        assert set(self._tools(mock_proxy_client)) == {
            session.LIST_NOTEBOOK_TOOLS_NAME,
            session.CALL_NOTEBOOK_TOOL_NAME,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name, arguments",
        [
            (session.LIST_NOTEBOOK_TOOLS_NAME, {}),
            (session.CALL_NOTEBOOK_TOOL_NAME, {"name": "get_cells"}),
        ],
    )
    async def test_not_connected(self, mock_proxy_client, name, arguments):
        tool = self._tools(mock_proxy_client)[name]
        with pytest.raises(ToolError, match="No Colab tab is connected"):
            await tool.run(arguments)

    @pytest.mark.asyncio
    async def test_list_notebook_tools(self, mock_proxy_client):
        client = self._connected_proxy_client(mock_proxy_client)
        schema = {"type": "object", "properties": {"code": {"type": "string"}}}
        client.list_tools = AsyncMock(
            return_value=[
                McpTool(name="add_code_cell", description="Adds", inputSchema=schema)
            ]
        )
        tool = self._tools(mock_proxy_client)[session.LIST_NOTEBOOK_TOOLS_NAME]

        result = await tool.run({})

        assert result.structured_content == {
            "result": [
                {"name": "add_code_cell", "description": "Adds", "inputSchema": schema}
            ]
        }

    @pytest.mark.asyncio
    async def test_call_notebook_tool(self, mock_proxy_client):
        client = self._connected_proxy_client(mock_proxy_client)
        client.call_tool_mcp = AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text="added")],
                structuredContent={"cellId": "c1"},
            )
        )
        tool = self._tools(mock_proxy_client)[session.CALL_NOTEBOOK_TOOL_NAME]

        result = await tool.run({"name": "add_code_cell", "arguments": {"code": "1+1"}})

        client.call_tool_mcp.assert_called_once_with(
            name="add_code_cell", arguments={"code": "1+1"}
        )
        assert [c.text for c in result.content] == ["added"]
        assert result.structured_content == {"cellId": "c1"}

    @pytest.mark.asyncio
    async def test_call_notebook_tool_without_arguments(self, mock_proxy_client):
        client = self._connected_proxy_client(mock_proxy_client)
        client.call_tool_mcp = AsyncMock(
            return_value=CallToolResult(content=[TextContent(type="text", text="[]")])
        )
        tool = self._tools(mock_proxy_client)[session.CALL_NOTEBOOK_TOOL_NAME]

        await tool.run({"name": "get_cells"})

        client.call_tool_mcp.assert_called_once_with(name="get_cells", arguments={})

    @pytest.mark.asyncio
    async def test_call_notebook_tool_error_result(self, mock_proxy_client):
        client = self._connected_proxy_client(mock_proxy_client)
        client.call_tool_mcp = AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text="no such cell")], isError=True
            )
        )
        tool = self._tools(mock_proxy_client)[session.CALL_NOTEBOOK_TOOL_NAME]

        with pytest.raises(ToolError, match="no such cell"):
            await tool.run({"name": "delete_cell", "arguments": {"cellId": "x"}})


class TestColabProxyClient:
    def test_is_connected(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.is_connected() is False
        mock_wss.connection_live.set()
        assert client.is_connected() is False
        client.proxy_mcp_client = Mock()
        assert client.is_connected() is True

    def test_client_factory_connection_live(self, mock_wss):
        mock_wss.connection_live.set()
        client = session.ColabProxyClient(mock_wss)
        client.proxy_mcp_client = Mock()

        assert client.client_factory() is client.proxy_mcp_client

    def test_client_factory_connection_not_live(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.client_factory() is client.stubbed_mcp_client

    @pytest.mark.asyncio
    async def test_await_proxy_connection(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        client._start_task = asyncio.create_task(asyncio.sleep(0.01))
        mock_wss.connection_live.set()
        with patch("colab_mcp.session.UI_CONNECTION_TIMEOUT", 0.1):
            await client.await_proxy_connection()
        await client._start_task

    @pytest.mark.asyncio
    @patch("colab_mcp.session.Client")
    @patch("colab_mcp.session.ColabTransport", spec=session.ColabTransport)
    async def test_start_proxy_client(
        self, mock_colab_transport, mock_client, mock_wss
    ):
        mock_client.return_value.__aenter__ = AsyncMock()
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        async with client:
            await client._start_task

        mock_colab_transport.assert_called_once_with(mock_wss)
        mock_client.assert_called_with(mock_colab_transport.return_value)


class TestColabTransport:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ClientSession")
    async def test_connect_session(self, mock_client_session, mock_wss):
        transport = session.ColabTransport(mock_wss)
        mock_client_session.return_value.__aenter__ = AsyncMock()
        async with transport.connect_session(foo="bar") as client_session:
            assert (
                client_session
                == mock_client_session.return_value.__aenter__.return_value
            )

        mock_client_session.assert_called_once_with(
            mock_wss.read_stream, mock_wss.write_stream, foo="bar"
        )


class TestColabSessionProxy:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ToolInjectionMiddleware")
    @patch("colab_mcp.session.ColabWebSocketServer")
    @patch("colab_mcp.session.ColabProxyClient")
    @patch("colab_mcp.session.ColabProxyMiddleware")
    async def test_start_proxy_server(
        self,
        mock_colab_proxy_middleware,
        mock_colab_proxy_client,
        mock_colab_web_socket_server,
        mock_tool_injection_middleware,
    ):
        mock_colab_web_socket_server.return_value.__aenter__ = AsyncMock()
        mock_colab_proxy_client.return_value.__aenter__ = AsyncMock()
        proxy = session.ColabSessionProxy()
        await proxy.start_proxy_server()
        mock_colab_proxy_client.assert_called_once()
        assert proxy.proxy_server is not None
        mock_colab_proxy_middleware.assert_called_once()
        mock_tool_injection_middleware.assert_called_once()
        injected = mock_tool_injection_middleware.call_args.kwargs["tools"]
        assert [tool.name for tool in injected] == [
            session.INJECTED_TOOL_NAME,
            session.LIST_NOTEBOOK_TOOLS_NAME,
            session.CALL_NOTEBOOK_TOOL_NAME,
        ]

    @pytest.mark.asyncio
    async def test_cleanup(self):
        proxy = session.ColabSessionProxy()
        proxy._exit_stack = AsyncMock()
        await proxy.cleanup()
        proxy._exit_stack.aclose.assert_called_once()
