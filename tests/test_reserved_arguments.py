"""Reserved AgntID arguments: a caller can never supply them, at any entry point.

Every test here is LOCAL. The runtime is the task fixture server in
conftest.py, which is NOT AgntID. Its tools copy the schema shape of the live
runtime (optional agntid_task_id / agntid_delegation_id / agntid_approval
parameters), but the fixture enforces nothing. A passing test proves that
PatchPilot refuses these arguments before anything is sent. It proves nothing
about what a live AgntID runtime would do with them.

Why the names are reserved (pinned sources, read for this pass):

- agntid SDK f4fcd2fe, mcp_wrapper.py: wrap_client adds its `task_argument`
  (default `_task_id`; PatchPilot passes `agntid_task_id`, the name the
  runtime reads) and `_delegation_id` when a delegation is set, after the
  caller's arguments.
  Response fields starting with `__agntid_` are platform fields.
- achr-runtime, tool_execution.py `_resolve_task_for_execution`: pops
  `agntid_task_id`, `agntid_delegation_id`, and `agntid_approval` from the
  call. The first two choose the task or delegation the call is attributed to.
  `agntid_approval` is merged into the policy context as approval evidence.
  None of them is a business input.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

import agntid
import pytest

from patchpilot.agntid_connector import ConfigError, ConnectorConfig, discover_tools
from patchpilot.github_capabilities import (
    CapabilityMapping,
    OperationStatus,
    build_tool_arguments,
    execute_capability,
    parse_capability_map,
    schema_fingerprint,
    validate_mapping,
)
from patchpilot.github_preflight_cli import run as preflight_run
from patchpilot.task_session import (
    TASK_ARGUMENT,
    CallStatus,
    TaskIdentity,
    is_reserved_argument,
    open_task_session,
)
from patchpilot.web import operations

from .conftest import FIXTURE_NOTE, fixture_accepted, fixture_denial
from .web_helpers import agntid_env, last_event, make_settings, serve_web

IDENTITY = TaskIdentity(agent_id="patchpilot-tests:v1", user_id="user-7f3a91")
IDENTITY_ENV = {"PATCHPILOT_AGENT_ID": IDENTITY.agent_id, "PATCHPILOT_USER_ID": IDENTITY.user_id}
PROMPT = "Read ticket T-1"

# Stands in for approval material and spoofed IDs. It must never be echoed.
SECRET = "fixture-approval-secret-5be1"
APPROVAL = {"required": True, "decision": "approved", "decision_id": SECRET, "approver_id": "fixture-approver"}

RESERVED = [
    # The live runtime's Task Glue arguments (task, delegation, approval).
    ("agntid_task_id", SECRET),
    ("agntid_delegation_id", SECRET),
    ("agntid_approval", APPROVAL),
    # The same namespace with other case or leading underscores.
    ("AGNTID_TASK_ID", SECRET),
    ("Agntid_Approval", APPROVAL),
    ("_agntid_task_id", SECRET),
    ("__agntid_result", SECRET),
    # The SDK wrapper's own correlation arguments.
    ("_task_id", SECRET),
    ("_delegation_id", SECRET),
]
RESERVED_IDS = [name for name, _ in RESERVED]


def _config(server) -> ConnectorConfig:
    return ConnectorConfig(server.url, timeout_seconds=10)


def _client(server):
    return agntid.AgntidMCPClient(server.url)


# --------------------------------------------------------------------------- #
# The rule (no network)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", RESERVED_IDS)
def test_reserved_names_match_the_rule(name):
    assert is_reserved_argument(name)


@pytest.mark.parametrize(
    "name",
    ["ticket_id", "path", "task_id", "delegation_id", "approval", "agntid", "agntidtask", "my_agntid_task_id"],
)
def test_ordinary_names_are_not_reserved(name):
    assert not is_reserved_argument(name)


# --------------------------------------------------------------------------- #
# Module 2 dispatcher (real SDK over real HTTP, fixture server)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("name", "value"), RESERVED, ids=RESERVED_IDS)
async def test_dispatcher_rejects_reserved_arguments_before_any_call(context_task_server, caplog, name, value):
    caplog.set_level(logging.DEBUG)
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            # The tool's own schema lists agntid_* parameters, as live schemas do.
            schema = next(t for t in session.tools if t.name == "fixture_get_ticket").input_schema
            assert {"agntid_task_id", "agntid_delegation_id", "agntid_approval"} <= set(schema["properties"])
            result = await session.call_tool("fixture_get_ticket", {"ticket_id": "T-1", name: value})
    assert result.status is CallStatus.REJECTED and not result.call_sent
    assert "reserved" in result.message and repr(name) in result.message
    assert "not an AgntID decision" in result.message
    assert result.denial is None and result.denial_explanation is None
    assert server.business_calls() == []
    for text in (result.message, repr(result), caplog.text):
        assert SECRET not in text


async def test_every_reserved_argument_is_named_and_the_input_is_not_changed(context_task_server):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    arguments = {"ticket_id": "T-1", "agntid_task_id": SECRET, "agntid_approval": dict(APPROVAL)}
    before = json.dumps(arguments, sort_keys=True)
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await session.call_tool("fixture_get_ticket", arguments)
    assert result.status is CallStatus.REJECTED
    assert "'agntid_approval'" in result.message and "'agntid_task_id'" in result.message
    assert json.dumps(arguments, sort_keys=True) == before  # nothing removed or rewritten
    assert server.business_calls() == []


async def test_ordinary_call_succeeds_and_only_the_sdk_adds_task_context(context_task_server):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await session.call_tool("fixture_get_ticket", {"ticket_id": "T-1"})
    assert result.ok
    assert result.value["ticket_id"] == "T-1"
    assert result.value["agntidArgumentsReceived"] == []
    # agntid_task_id is added by agntid.wrap_client after the dispatcher's check.
    assert server.business_calls() == [("fixture_get_ticket", {"ticket_id": "T-1", "agntid_task_id": session.task_id})]


def test_task_argument_is_the_runtime_name_and_reserved():
    # achr-runtime tool_execution.py: _TASK_CORRELATION_ARG = "agntid_task_id".
    assert TASK_ARGUMENT == "agntid_task_id"
    assert is_reserved_argument(TASK_ARGUMENT)
    # The SDK contract relied on: wrap_client accepts task_argument (keyword-only).
    import inspect

    param = inspect.signature(agntid.wrap_client).parameters["task_argument"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


async def test_wire_carries_runtime_task_argument_not_sdk_default(context_task_server):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            first = await session.call_tool("fixture_get_ticket", {"ticket_id": "T-1"})
            second = await session.call_tool("fixture_get_ticket", {"ticket_id": "T-2"})
    assert first.ok and second.ok
    sent = server.called("fixture_get_ticket")
    assert [args.get("agntid_task_id") for args in sent] == [session.task_id, session.task_id]
    assert all("_task_id" not in args and "_delegation_id" not in args for args in sent)


@pytest.mark.parametrize("name", ["agntid_task_id", "_task_id", "AGNTID_TASK_ID"])
async def test_caller_cannot_choose_the_task_a_call_is_attributed_to(context_task_server, name):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await session.call_tool("fixture_get_ticket", {"ticket_id": "T-1", name: "spoofed-task"})
    assert result.status is CallStatus.REJECTED and not result.call_sent
    assert "spoofed-task" not in result.message
    assert server.business_calls() == []


async def test_local_rejection_and_fixture_denial_stay_distinct(context_task_server):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    server.denials["fixture_get_ticket"] = fixture_denial("fixture: ticket needs approval", "fixture-policy")
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            rejected = await session.call_tool("fixture_get_ticket", {"ticket_id": "T-1", "agntid_approval": APPROVAL})
            denied = await session.call_tool("fixture_get_ticket", {"ticket_id": "T-1"})
    assert rejected.status is CallStatus.REJECTED and rejected.denial is None
    assert "not an AgntID decision" in rejected.message
    assert denied.status is CallStatus.DENIED
    assert denied.denial["reason"] == "fixture: ticket needs approval"
    assert denied.denial["policy_name"] == "fixture-policy"
    # One call reached the server: the denied one, once, with SDK context only.
    assert server.called("fixture_get_ticket") == [{"ticket_id": "T-1", "agntid_task_id": session.task_id}]


# --------------------------------------------------------------------------- #
# Module 3 capability mappings
# --------------------------------------------------------------------------- #


def _issue_map_entry(tool, **overrides) -> dict:
    entry = {
        "tool": tool.name,
        "schema_sha256": schema_fingerprint(tool.input_schema),
        "arguments": {"owner": "owner", "repo": "repo", "issue_number": "issue_number"},
    }
    entry.update(overrides)
    return entry


async def _context_issue_tool(server):
    tools = (await discover_tools(_config(server))).tools
    return next(t for t in tools if t.name == "fixture_github_issue_context_read")


ISSUE_PARAMS = {"owner": "fixture-org", "repo": "fixture-repo", "issue_number": 7}


@pytest.mark.parametrize(
    "overrides",
    [
        {"arguments": {"owner": "agntid_task_id", "repo": "repo", "issue_number": "issue_number"}},
        {"arguments": {"owner": "owner", "repo": "agntid_delegation_id", "issue_number": "issue_number"}},
        {"fixed_arguments": {"agntid_approval": APPROVAL}},
        {"fixed_arguments": {"agntid_task_id": SECRET}},
        {"fixed_arguments": {"_task_id": SECRET}},
    ],
    ids=["param->agntid_task_id", "param->agntid_delegation_id", "fixed agntid_approval", "fixed agntid_task_id",
         "fixed _task_id"],
)
async def test_capability_map_cannot_name_a_reserved_argument(context_task_server, overrides):
    tool = await _context_issue_tool(context_task_server)
    data = {"capabilities": {"get_issue": _issue_map_entry(tool, **overrides)}}
    with pytest.raises(ConfigError, match="reserved AgntID argument") as info:
        parse_capability_map(data, source="fixture-map.json")
    assert SECRET not in str(info.value)


@pytest.mark.parametrize(
    ("arguments", "fixed"),
    [
        ({"owner": "agntid_task_id", "repo": "repo", "issue_number": "issue_number"}, {}),
        ({"owner": "owner", "repo": "repo", "issue_number": "issue_number"}, {"agntid_approval": APPROVAL}),
    ],
    ids=["param->agntid_task_id", "fixed agntid_approval"],
)
async def test_mapping_built_without_the_parser_still_sends_nothing(context_task_server, arguments, fixed):
    """A mapping object made in code skips parse_capability_map; the call-time checks still hold."""
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_context_read")
    tool = await _context_issue_tool(server)
    mapping = CapabilityMapping("get_issue", tool.name, schema_fingerprint(tool.input_schema), arguments, fixed)
    assert any("reserved AgntID argument" in p for p in validate_mapping(mapping, [tool]).problems)
    built, problems = build_tool_arguments(mapping, tool, ISSUE_PARAMS)
    assert built is None and any("reserved AgntID argument" in p for p in problems)
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await execute_capability(session, {"get_issue": mapping}, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
    assert result.status is OperationStatus.INVALID_MAPPING and not result.call_sent
    assert server.business_calls() == []
    assert SECRET not in json.dumps(result.to_dict())


async def test_capability_parameters_cannot_carry_reserved_arguments(context_task_server):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_context_read")
    tool = await _context_issue_tool(server)
    mappings = parse_capability_map({"capabilities": {"get_issue": _issue_map_entry(tool)}}, source="fixture-map.json")
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await execute_capability(
                session, mappings, "get_issue", {**ISSUE_PARAMS, "agntid_task_id": SECRET}, timeout_seconds=10
            )
    assert result.status is OperationStatus.INVALID_ARGUMENTS and not result.call_sent
    assert server.business_calls() == []
    assert SECRET not in json.dumps(result.to_dict())


async def test_valid_mapping_on_a_tool_listing_reserved_parameters_still_works(context_task_server):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_context_read")
    tool = await _context_issue_tool(server)
    mappings = parse_capability_map({"capabilities": {"get_issue": _issue_map_entry(tool)}}, source="fixture-map.json")
    assert validate_mapping(mappings["get_issue"], [tool]).ok
    async with _client(server) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await execute_capability(session, mappings, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
    assert result.ok
    assert result.value["note"] == FIXTURE_NOTE and result.value["agntidArgumentsReceived"] == []
    assert server.business_calls() == [
        ("fixture_github_issue_context_read", {**ISSUE_PARAMS, "agntid_task_id": session.task_id})
    ]


# --------------------------------------------------------------------------- #
# patchpilot-github-preflight CLI
# --------------------------------------------------------------------------- #


def _cli(argv, environ, tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    code = preflight_run([*argv, "--env-file", str(tmp_path / "absent.env")], environ=environ, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _write_map(server, tmp_path: Path, **overrides) -> Path:
    tool = asyncio.run(_context_issue_tool(server))
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"capabilities": {"get_issue": _issue_map_entry(tool, **overrides)}}))
    return path


RUN_ISSUE = ["--task-prompt", "Read issue 7", "--run", "get_issue",
             "--arg", "owner=fixture-org", "--arg", "repo=fixture-repo", "--arg", "issue_number=7"]


@pytest.mark.parametrize("name", ["agntid_task_id", "agntid_delegation_id", "agntid_approval", "_task_id"])
def test_cli_arg_cannot_supply_a_reserved_argument(context_task_server, tmp_path, name):
    server = context_task_server
    mapping = _write_map(server, tmp_path)
    env = {"AGNTID_MCP_URL": server.url, **IDENTITY_ENV}
    for extra in ([], ["--json"]):
        code, out, err = _cli(["--mapping", str(mapping), *RUN_ISSUE, "--arg", f"{name}={SECRET}", *extra], env, tmp_path)
        assert code == 2
        assert SECRET not in out and SECRET not in err
    assert server.calls == []  # no task opened, nothing sent


def test_cli_mapping_file_cannot_supply_a_reserved_argument(context_task_server, tmp_path):
    server = context_task_server
    mapping = _write_map(server, tmp_path, fixed_arguments={"agntid_approval": APPROVAL})
    env = {"AGNTID_MCP_URL": server.url, **IDENTITY_ENV}
    for extra in ([], ["--json"]):
        code, out, err = _cli(["--mapping", str(mapping), *RUN_ISSUE, *extra], env, tmp_path)
        assert code == 2
        assert "reserved AgntID argument" in out + err
        assert SECRET not in out and SECRET not in err
    assert server.calls == []


def test_cli_ordinary_run_still_reads_once(context_task_server, tmp_path):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_context_read")
    mapping = _write_map(server, tmp_path)
    env = {"AGNTID_MCP_URL": server.url, **IDENTITY_ENV}
    code, out, err = _cli(["--mapping", str(mapping), *RUN_ISSUE, "--json"], env, tmp_path)
    assert code == 0, err
    assert json.loads(out)["task"]["run"]["status"] == "success"
    [(name, args)] = server.business_calls()
    assert name == "fixture_github_issue_context_read"
    # The only agntid_* argument on the wire is the SDK-injected task ID.
    assert [key for key in args if is_reserved_argument(key)] == [TASK_ARGUMENT]


# --------------------------------------------------------------------------- #
# Browser (POST /api/runs kind=probe)
# --------------------------------------------------------------------------- #


def _probe(web, arguments):
    call = {"tool": "fixture_get_ticket", "arguments": arguments, "confirmUnannotated": False}
    return web.run("probe", prompt=PROMPT, call=call)


@pytest.mark.parametrize(("name", "value"), RESERVED, ids=RESERVED_IDS)
def test_browser_probe_refuses_reserved_arguments_and_withholds_values(context_task_server, caplog, name, value):
    caplog.set_level(logging.DEBUG)
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    with serve_web(make_settings(agntid_env(server.url))) as web:
        run = _probe(web, {"ticket_id": "T-1", name: value})
        listed = web.get("/api/runs").text
    assert run["outcome"] == "refused"
    refusal = last_event(run, "probe_check")
    assert (refusal["source"], refusal["status"]) == ("patchpilot", "error")
    assert server.calls == []  # refused before a task was opened
    assert run["request"]["call"]["arguments"]["ticket_id"] == "T-1"
    assert name in run["request"]["call"]["arguments"]  # the name is shown; its value is withheld
    for text in (json.dumps(run), listed, caplog.text):
        assert SECRET not in text


def test_browser_probe_is_still_stopped_by_the_dispatcher_if_the_ui_check_is_bypassed(
    context_task_server, monkeypatch
):
    """Simulates a broken UI check: PatchPilot's core dispatcher must still refuse."""
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    monkeypatch.setattr(operations, "probe_problems", lambda tool, call: [])
    with serve_web(make_settings(agntid_env(server.url))) as web:
        run = _probe(web, {"ticket_id": "T-1", "agntid_approval": APPROVAL})
    assert run["outcome"] == "rejected"
    call = last_event(run, "call")
    assert (call["source"], call["status"]) == ("patchpilot", "error")
    assert call["detail"]["callSent"] is False and call["detail"]["decision"] == "not_observed"
    assert "not an AgntID decision" in call["detail"]["message"]
    assert [name for name, _ in server.calls] == ["agntid_task_open", "agntid_task_close"]
    assert SECRET not in json.dumps(run)


def test_browser_probe_ordinary_call_still_goes_through_the_task(context_task_server):
    server = context_task_server
    server.task_open_response = fixture_accepted("fixture_get_ticket")
    with serve_web(make_settings(agntid_env(server.url))) as web:
        run = _probe(web, {"ticket_id": "T-1"})
    assert run["outcome"] == "success"
    call = last_event(run, "call")
    assert call["detail"]["value"]["agntidArgumentsReceived"] == []
    task_id = call["detail"]["taskId"]
    assert server.business_calls() == [("fixture_get_ticket", {"ticket_id": "T-1", "agntid_task_id": task_id})]


@pytest.mark.parametrize(
    "body",
    [
        {"issue": "fixture-org/fixture-repo#7", "prompt": PROMPT, "agntid_task_id": SECRET},
        {"issue": "fixture-org/fixture-repo#7", "prompt": PROMPT, "files": [{"path": "a.py", "agntid_approval": SECRET}]},
    ],
)
def test_investigation_request_cannot_add_argument_names(closed_port_url, body):
    with serve_web(make_settings(agntid_env(closed_port_url))) as web:
        response = web.start("investigation", **body)
        runs = web.get("/api/runs").json()["runs"]
    assert response.status_code == 400 and runs == []
    assert SECRET not in response.text
