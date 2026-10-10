"""Discovery through the real SDK client against local fixture MCP servers.

These servers are NOT AgntID (see conftest.py). They prove the connector's
handling of real MCP/HTTP behavior, not that an AgntID runtime works.
"""

from __future__ import annotations

import time

import agntid
import fastmcp
import pytest
from mcp.types import Tool, ToolAnnotations

from patchpilot.agntid_connector import (
    AuthenticationError,
    ConnectorConfig,
    ConnectorTimeoutError,
    DiscoveryError,
    RuntimeProtocolError,
    RuntimeUnreachableError,
    discover_tools,
    normalize_tool,
)

from .conftest import FIXTURE_TOKEN


class CapturingFactory:
    """Builds the real agntid.AgntidMCPClient and keeps a reference to check cleanup."""

    def __init__(self):
        self.clients: list[agntid.AgntidMCPClient] = []

    def __call__(self, url, *, access_token=None):
        client = agntid.AgntidMCPClient(url, access_token=access_token)
        self.clients.append(client)
        return client


async def test_lists_tools_and_preserves_names_and_schemas(tool_server):
    factory = CapturingFactory()
    result = await discover_tools(ConnectorConfig(tool_server.url), client_factory=factory)

    # Ground truth: what a plain fastmcp client (no PatchPilot code) receives.
    async with fastmcp.Client(tool_server.url) as raw_client:
        raw = {t.name: t for t in await raw_client.list_tools()}

    assert [t.name for t in result.tools] == list(raw)
    for tool in result.tools:
        assert tool.input_schema == raw[tool.name].inputSchema
        assert tool.description == raw[tool.name].description
        assert tool.title == raw[tool.name].title
        assert not tool.is_runtime_control

    note = next(t for t in result.tools if t.name == "fixture_srv__create_note")
    assert note.title == "Fixture note writer"
    assert note.description == "Store a structured note.\nSecond line."
    assert set(note.input_schema["properties"]) == {"note", "priority"}
    assert note.input_schema["required"] == ["note"]
    nested = note.input_schema["properties"]["note"]  # nested model schema kept intact
    assert set(nested["properties"]) == {"title", "tags"}
    assert nested["properties"]["tags"]["items"] == {"type": "string"}

    no_desc = next(t for t in result.tools if t.name == "fixture_no_description")
    assert no_desc.description is None

    assert result.endpoint == tool_server.url
    assert result.authenticated is False
    assert result.sdk_version.startswith("0.2.0")
    assert not factory.clients[0].is_connected()


async def test_empty_tool_list_is_reported_as_empty(empty_server):
    result = await discover_tools(ConnectorConfig(empty_server.url))
    assert result.tools == ()
    assert result.to_dict()["toolCount"] == 0


async def test_token_is_sent_as_bearer_on_the_transport(auth_server):
    result = await discover_tools(ConnectorConfig(auth_server.url, access_token=FIXTURE_TOKEN))
    assert len(result.tools) == 3
    assert result.authenticated is True
    assert auth_server.auth_headers
    assert set(auth_server.auth_headers) == {f"Bearer {FIXTURE_TOKEN}"}


async def test_missing_token_is_an_authentication_error(auth_server):
    with pytest.raises(AuthenticationError, match="401") as info:
        await discover_tools(ConnectorConfig(auth_server.url))
    assert info.value.phase == "connect"
    assert "AGNTID_ACCESS_TOKEN" in info.value.message


async def test_wrong_token_is_rejected_and_never_leaks(auth_server):
    secret = "wrong-but-secret-token-91f3"
    factory = CapturingFactory()
    with pytest.raises(AuthenticationError) as info:
        await discover_tools(ConnectorConfig(auth_server.url, access_token=secret), client_factory=factory)
    error = info.value
    assert "rejected" in error.message
    for text in (str(error), repr(error), error.message, error.cause_type or ""):
        assert secret not in text
    # The original exception (whose request carries the header) is not chained.
    assert error.__cause__ is None and error.__context__ is None
    assert not factory.clients[0].is_connected()


async def test_connection_refused_is_unreachable(closed_port_url):
    with pytest.raises(RuntimeUnreachableError) as info:
        await discover_tools(ConnectorConfig(closed_port_url, timeout_seconds=10))
    assert info.value.phase == "connect"
    assert info.value.cause_type == "httpx.ConnectError"


async def test_unresolvable_host_is_unreachable():
    with pytest.raises(RuntimeUnreachableError):
        await discover_tools(ConnectorConfig("http://patchpilot-no-such-host.invalid/mcp", timeout_seconds=10))


async def test_wrong_path_is_a_protocol_error(tool_server):
    wrong = tool_server.url.replace("/mcp", "/not-mcp")
    with pytest.raises(RuntimeProtocolError):
        await discover_tools(ConnectorConfig(wrong, timeout_seconds=10))


async def test_tools_list_failure_after_connect_is_a_discovery_error(failing_list_server):
    factory = CapturingFactory()
    with pytest.raises(DiscoveryError, match="tools/list rejected") as info:
        await discover_tools(ConnectorConfig(failing_list_server.url), client_factory=factory)
    assert info.value.phase == "discover"
    assert not factory.clients[0].is_connected()


async def test_unresponsive_runtime_times_out_within_bound(silent_server_url):
    factory = CapturingFactory()
    started = time.monotonic()
    with pytest.raises(ConnectorTimeoutError) as info:
        await discover_tools(ConnectorConfig(silent_server_url, timeout_seconds=1.0), client_factory=factory)
    assert time.monotonic() - started < 5
    assert info.value.phase == "connect"
    assert not factory.clients[0].is_connected()


def test_normalize_flags_runtime_control_tools_using_real_mcp_types():
    tool = Tool(
        name=agntid.TASK_OPEN_TOOL,
        description="control",
        inputSchema={"type": "object", "properties": {"task_id": {"type": "string"}}},
        annotations=ToolAnnotations(readOnlyHint=False),
    )
    info = normalize_tool(tool)
    assert info.name == "agntid_task_open"
    assert info.is_runtime_control
    assert info.annotations == {"readOnlyHint": False}


def test_normalize_refuses_nameless_tools():
    with pytest.raises(DiscoveryError):
        normalize_tool({"name": "", "inputSchema": {"type": "object"}})


def test_normalize_schema_is_a_copy():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    info = normalize_tool({"name": "x", "inputSchema": schema})
    assert info.input_schema == schema
    assert info.input_schema is not schema
