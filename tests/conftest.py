"""Local test servers for the connector tests.

IMPORTANT: these are NOT the AgntID runtime. They are real MCP servers (FastMCP,
the library the AgntID SDK client itself is built on) served over real HTTP on
127.0.0.1, so the tests exercise the real `agntid.AgntidMCPClient` and
`agntid.get_tools_list` code paths and real transport errors. Passing tests
prove the connector's handling of MCP responses; they prove nothing about a live
AgntID runtime. Use `patchpilot-discover` or `pytest -m live` for that.

All fixture tool names start with `fixture_` so they cannot be mistaken for
tools discovered from AgntID.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from patchpilot._warnings import silence_authlib_deprecations

silence_authlib_deprecations()

import pytest  # noqa: E402
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware import Middleware as McpMiddleware
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from pydantic import BaseModel, Field
from starlette.middleware import Middleware as AsgiMiddleware

FIXTURE_TOKEN = "fixture-token-7c1f0e2a9b"


class _AuthHeaderRecorder:
    """ASGI middleware recording the Authorization headers the server received."""

    def __init__(self, app, sink: list[str]):
        self.app = app
        self.sink = sink

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key == b"authorization":
                    self.sink.append(value.decode())
        await self.app(scope, receive, send)


@dataclass
class LocalServer:
    url: str
    auth_headers: list[str] = field(default_factory=list)


def _free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def _serve(mcp: FastMCP) -> Iterator[LocalServer]:
    auth_headers: list[str] = []
    app = mcp.http_app(
        path="/mcp",
        middleware=[AsgiMiddleware(_AuthHeaderRecorder, sink=auth_headers)],
    )
    sock = _free_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fixture MCP server did not start")
        time.sleep(0.02)
    try:
        yield LocalServer(url=f"http://127.0.0.1:{port}/mcp", auth_headers=auth_headers)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


class Note(BaseModel):
    title: str = Field(description="Short title")
    tags: list[str] = Field(default_factory=list, description="Labels")


def _tool_server(name: str, **kwargs) -> FastMCP:
    mcp = FastMCP(name, **kwargs)

    @mcp.tool(description="Add two numbers. Fixture tool, not an AgntID tool.")
    def fixture_add_numbers(first_number: float, second_number: float) -> float:
        return first_number + second_number

    @mcp.tool(title="Fixture note writer", description="Store a structured note.\nSecond line.")
    def fixture_srv__create_note(note: Note, priority: int = 3) -> str:
        return note.title

    @mcp.tool
    def fixture_no_description(path: str) -> str:
        return path

    return mcp


@pytest.fixture
def tool_server() -> Iterator[LocalServer]:
    """Open MCP server exposing three fixture tools."""
    yield from _serve(_tool_server("fixture-tools"))


@pytest.fixture
def empty_server() -> Iterator[LocalServer]:
    """Open MCP server exposing zero tools."""
    yield from _serve(FastMCP("fixture-empty"))


@pytest.fixture
def auth_server() -> Iterator[LocalServer]:
    """MCP server that enforces bearer auth; only FIXTURE_TOKEN is accepted."""
    verifier = StaticTokenVerifier(tokens={FIXTURE_TOKEN: {"client_id": "patchpilot-tests", "scopes": []}})
    yield from _serve(_tool_server("fixture-auth", auth=verifier))


class _RejectListTools(McpMiddleware):
    async def on_list_tools(self, context, call_next):
        raise McpError(ErrorData(code=-32603, message="fixture: tools/list rejected"))


@pytest.fixture
def failing_list_server() -> Iterator[LocalServer]:
    """MCP server whose initialize succeeds but tools/list returns a JSON-RPC error."""
    mcp = _tool_server("fixture-failing-list")
    mcp.add_middleware(_RejectListTools())
    yield from _serve(mcp)


@pytest.fixture
def closed_port_url() -> str:
    """URL of a 127.0.0.1 port with nothing listening."""
    sock = _free_socket()
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/mcp"


@pytest.fixture
def silent_server_url() -> Iterator[str]:
    """TCP listener that accepts connections and never answers."""
    sock = _free_socket()
    sock.listen(16)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept_loop() -> None:
        sock.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
                accepted.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    finally:
        stop.set()
        thread.join(timeout=5)
        for conn in accepted:
            conn.close()
        sock.close()


# --------------------------------------------------------------------------- #
# Task-lifecycle fixture server (Module 2)
# --------------------------------------------------------------------------- #
#
# NOT AgntID. A real FastMCP server over real HTTP that answers the SDK's
# task-control tool names (agntid_task_open / agntid_task_close) with payloads
# each test configures, and records every tools/call it receives. Its denials
# use the error envelope that the pinned SDK's agntid/denial.py parses; they are
# fixture output, never an AgntID policy decision. They show that PatchPilot
# handles the SDK contract, not that a real runtime enforces any policy.


def fixture_accepted(*tool_names: str) -> dict:
    """Task-open payload: accepted, intent prepared, shortlist = tool_names."""
    return {
        "ok": True,
        "intent": {
            "prepared": True,
            "snapshot": {"engine_id": "fixture-engine", "relevant_tool_names": list(tool_names)},
            "relevant_tool_count": len(tool_names),
            "no_relevant_tools": not tool_names,
        },
    }


def fixture_denial(reason: str, policy_name: str) -> dict:
    """Error envelope in the shape agntid/denial.py parses (fixture, not AgntID)."""
    return {
        "__agntid_error": "Request denied",
        "__agntid_policy_decision": {"allowed": False, "reason": reason, "policy_name": policy_name},
    }


@dataclass
class TaskFixtureServer:
    url: str = ""
    auth_headers: list[str] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    task_open_response: dict = field(default_factory=lambda: fixture_accepted("fixture_read_file"))
    task_open_error: str | None = None
    task_close_error: str | None = None
    denials: dict[str, dict] = field(default_factory=dict)
    delays: dict[str, float] = field(default_factory=dict)  # seconds to stall before answering

    def called(self, name: str) -> list[dict]:
        return [args for tool, args in self.calls if tool == name]

    def business_calls(self) -> list[tuple[str, dict]]:
        control = {"agntid_task_open", "agntid_task_close"}
        return [(tool, args) for tool, args in self.calls if tool not in control]


class _RecordCalls(McpMiddleware):
    """Records raw tools/call arguments, then pops agntid_task_id as the runtime does."""

    def __init__(self, state: TaskFixtureServer):
        self.state = state

    async def on_call_tool(self, context, call_next):
        import json

        from fastmcp.exceptions import ToolError
        from mcp.types import CallToolRequestParams

        name = context.message.name
        args = dict(context.message.arguments or {})
        self.state.calls.append((name, dict(args)))
        if name in self.state.denials:
            raise ToolError(json.dumps(self.state.denials[name]))
        if name in self.state.delays:
            import asyncio

            await asyncio.sleep(self.state.delays[name])
        args.pop("agntid_task_id", None)
        message = CallToolRequestParams(name=name, arguments=args)
        return await call_next(context.copy(message=message))


def _task_fixture_mcp(state: TaskFixtureServer, **kwargs) -> FastMCP:
    from fastmcp.exceptions import ToolError

    mcp = FastMCP("fixture-task-runtime", **kwargs)
    mcp.add_middleware(_RecordCalls(state))

    @mcp.tool(name="agntid_task_open", description="Fixture stand-in, not AgntID.")
    def task_open(task_id: str, agent_id: str, user_id: str, prompt: str) -> dict:
        if state.task_open_error:
            raise ToolError(state.task_open_error)
        return state.task_open_response

    @mcp.tool(name="agntid_task_close", description="Fixture stand-in, not AgntID.")
    def task_close(task_id: str) -> dict:
        if state.task_close_error:
            raise ToolError(state.task_close_error)
        return {"ok": True}

    @mcp.tool(description="Fixture tool, not an AgntID tool.")
    def fixture_read_file(path: str) -> str:
        return f"fixture contents of {path}"

    @mcp.tool(description="Fixture tool, not an AgntID tool.")
    def fixture_write_file(path: str, content: str) -> dict:
        return {"written": path, "bytes": len(content)}

    @mcp.tool(description="Fixture tool, not an AgntID tool.")
    def fixture_add_numbers(first_number: float, second_number: float) -> float:
        return first_number + second_number

    @mcp.tool(description="Fixture tool that always fails.")
    def fixture_explode() -> str:
        raise ValueError("fixture backend failure")

    return mcp


def _serve_task_fixture(register=None, **kwargs) -> Iterator[TaskFixtureServer]:
    state = TaskFixtureServer()
    mcp = _task_fixture_mcp(state, **kwargs)
    if register is not None:
        register(mcp)
    server = _serve(mcp)
    local = next(server)
    state.url = local.url
    state.auth_headers = local.auth_headers
    try:
        yield state
    finally:
        server.close()  # runs _serve's shutdown


@pytest.fixture
def task_server() -> Iterator[TaskFixtureServer]:
    """Open fixture server answering the SDK task-control tools (NOT AgntID)."""
    yield from _serve_task_fixture()


@pytest.fixture
def auth_task_server() -> Iterator[TaskFixtureServer]:
    """Same as task_server, but requires bearer FIXTURE_TOKEN (NOT AgntID)."""
    verifier = StaticTokenVerifier(tokens={FIXTURE_TOKEN: {"client_id": "patchpilot-tests", "scopes": []}})
    yield from _serve_task_fixture(auth=verifier)


# --------------------------------------------------------------------------- #
# GitHub-shaped fixture tools (Module 3)
# --------------------------------------------------------------------------- #
#
# NOT GitHub and NOT AgntID. Names, schemas, and results are invented for the
# tests, start with fixture_, and say so in their output. They exist only to
# exercise PatchPilot's schema parsing, mapping validation, and dispatch. No
# real GitHub tool name or schema is implied.

FIXTURE_NOTE = "fixture data, not from GitHub"


def register_github_fixture_tools(mcp: FastMCP) -> None:
    read_only = {"readOnlyHint": True}

    @mcp.tool(annotations=read_only, description="Fixture GitHub-shaped issue reader. Not GitHub.")
    def fixture_github_issue_read(
        method: Literal["get", "get_comments"], owner: str, repo: str, issue_number: int
    ) -> dict:
        return {"note": FIXTURE_NOTE, "repo": f"{owner}/{repo}", "number": issue_number, "method": method}

    @mcp.tool(annotations=read_only, description="Fixture GitHub-shaped repository reader. Not GitHub.")
    def fixture_github_get_repository(owner: str, repo: str) -> dict:
        return {"note": FIXTURE_NOTE, "full_name": f"{owner}/{repo}"}

    @mcp.tool(annotations=read_only, description="Fixture GitHub-shaped file reader. Not GitHub.")
    def fixture_github_get_file_contents(owner: str, repo: str, path: str, ref: str | None = None) -> str:
        return f"{FIXTURE_NOTE}: {owner}/{repo}/{path}@{ref or 'default'}"

    @mcp.tool(annotations=read_only, description="Fixture GitHub-shaped reader that always fails. Not GitHub.")
    def fixture_github_broken_read(owner: str, repo: str) -> dict:
        raise ValueError("fixture backend failure")

    @mcp.tool(annotations={"readOnlyHint": False}, description="Fixture GitHub-shaped issue writer. Not GitHub.")
    def fixture_github_create_issue(owner: str, repo: str, title: str) -> dict:
        return {"note": FIXTURE_NOTE, "created": title}


@pytest.fixture
def github_task_server() -> Iterator[TaskFixtureServer]:
    """Task fixture server plus GitHub-shaped fixture tools (NOT AgntID, NOT GitHub)."""
    yield from _serve_task_fixture(register_github_fixture_tools)


@pytest.fixture
def auth_github_task_server() -> Iterator[TaskFixtureServer]:
    """github_task_server that requires bearer FIXTURE_TOKEN."""
    verifier = StaticTokenVerifier(tokens={FIXTURE_TOKEN: {"client_id": "patchpilot-tests", "scopes": []}})
    yield from _serve_task_fixture(register_github_fixture_tools, auth=verifier)


# --------------------------------------------------------------------------- #
# Reserved AgntID arguments in tool schemas (hardening pass)
# --------------------------------------------------------------------------- #
#
# NOT AgntID. The live runtime (achr-runtime tool_prefix_handler.py) adds three
# optional parameters to every mounted tool's schema: agntid_task_id,
# agntid_delegation_id, and agntid_approval. It pops them before policy
# evaluation (tool_execution.py _resolve_task_for_execution). These fixture
# tools copy only that schema shape, so tests can show PatchPilot's checks hold
# when a tool's own schema lists those names. They report which agntid_*
# arguments reached them; the fixture enforces nothing.


def _agntid_received(**values) -> list[str]:
    return sorted(name for name, value in values.items() if value is not None)


def register_context_fixture_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        annotations={"readOnlyHint": True},
        description="Fixture ticket reader with the live runtime's context parameters. Not AgntID.",
    )
    def fixture_get_ticket(
        ticket_id: str,
        agntid_task_id: str | None = None,
        agntid_delegation_id: str | None = None,
        agntid_approval: dict | None = None,
    ) -> dict:
        return {
            "note": "fixture data, not AgntID",
            "ticket_id": ticket_id,
            "agntidArgumentsReceived": _agntid_received(
                agntid_task_id=agntid_task_id,
                agntid_delegation_id=agntid_delegation_id,
                agntid_approval=agntid_approval,
            ),
        }

    @mcp.tool(
        annotations={"readOnlyHint": True},
        description="Fixture GitHub-shaped issue reader with the live runtime's context parameters. "
        "Not GitHub, not AgntID.",
    )
    def fixture_github_issue_context_read(
        owner: str,
        repo: str,
        issue_number: int,
        agntid_task_id: str | None = None,
        agntid_delegation_id: str | None = None,
        agntid_approval: dict | None = None,
    ) -> dict:
        return {
            "note": FIXTURE_NOTE,
            "repo": f"{owner}/{repo}",
            "number": issue_number,
            "agntidArgumentsReceived": _agntid_received(
                agntid_task_id=agntid_task_id,
                agntid_delegation_id=agntid_delegation_id,
                agntid_approval=agntid_approval,
            ),
        }


@pytest.fixture
def context_task_server() -> Iterator[TaskFixtureServer]:
    """Task fixture server whose tools list agntid_* schema parameters (NOT AgntID)."""
    yield from _serve_task_fixture(register_context_fixture_tools)
