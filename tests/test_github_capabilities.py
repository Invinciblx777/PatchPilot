"""GitHub capability discovery, mapping validation, and dispatch.

Two kinds of test live here, both LOCAL:

- Metadata unit tests: tool metadata dicts written for the tests, parsed with
  Module 1's normalize_tool. No network. They are not a real GitHub tool list.
- SDK transport tests: the real AgntID SDK over real HTTP against the task
  fixture server plus GitHub-shaped fixture tools (conftest.py). That server
  is NOT AgntID and NOT GitHub; its tools start with fixture_ and return data
  that says it is fixture data.

Nothing here shows that a real AgntID runtime exposes a GitHub tool, opened a
task, or authorized or denied a GitHub read.
"""

from __future__ import annotations

import copy
import json

import agntid
import fastmcp
import pytest

from patchpilot.agntid_connector import ConfigError, ConnectorConfig, discover_tools, normalize_tool
from patchpilot.github_capabilities import (
    CapabilityMapping,
    OperationStatus,
    build_tool_arguments,
    execute_capability,
    find_candidates,
    load_capability_map,
    operation_status,
    parse_capability_map,
    schema_fingerprint,
    validate_mapping,
)
from patchpilot.task_session import CallStatus, TaskIdentity, TaskOpenError, ToolCallResult, open_task_session

from .conftest import FIXTURE_NOTE, fixture_accepted, fixture_denial

IDENTITY = TaskIdentity(agent_id="patchpilot-tests:v1", user_id="user-7f3a91")
PROMPT = "Read issue 7 in fixture-org/fixture-repo"

ISSUE_SCHEMA = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": ["get", "get_comments"]},
        "owner": {"type": "string"},
        "repo": {"type": "string"},
        "issue_number": {"type": "integer"},
    },
    "required": ["method", "owner", "repo", "issue_number"],
}


def _tool(name="fixture_srv.issue_read", schema=None, annotations=None, description="Fixture GitHub issue reader"):
    return normalize_tool(
        {
            "name": name,
            "description": description,
            "inputSchema": copy.deepcopy(schema or ISSUE_SCHEMA),
            "annotations": {"readOnlyHint": True} if annotations is None else annotations,
        }
    )


def _issue_mapping(info, **overrides) -> CapabilityMapping:
    fields = dict(
        capability="get_issue",
        tool=info.name,
        schema_sha256=schema_fingerprint(info.input_schema),
        arguments={"owner": "owner", "repo": "repo", "issue_number": "issue_number"},
        fixed_arguments={"method": "get"},
        source="test",
    )
    fields.update(overrides)
    return CapabilityMapping(**fields)


# --------------------------------------------------------------------------- #
# Candidate discovery (metadata unit tests)
# --------------------------------------------------------------------------- #


def test_candidates_preserve_exact_prefixed_names_and_exclude_writes_and_controls():
    tools = [
        _tool("fixture_srv.issue_read"),
        _tool("fixture_srv.get_file_contents", {"type": "object", "properties": {"owner": {}, "repo": {}}}),
        _tool("fixture_srv.create_issue", annotations={"readOnlyHint": False}),
        _tool(agntid.TASK_OPEN_TOOL, description="github"),
        _tool("fixture_add_numbers", {"type": "object", "properties": {"a": {"type": "number"}}}, description="math"),
    ]
    candidates = find_candidates(tools)
    assert [t.name for t in candidates["get_issue"]] == ["fixture_srv.issue_read"]
    assert [t.name for t in candidates["read_file"]] == ["fixture_srv.get_file_contents"]
    assert candidates["get_repository"] == ()  # missing capability is reported as missing


def test_multiple_candidates_are_reported_not_chosen():
    tools = [_tool("fixture_a.get_issue"), _tool("fixture_b.issue_read")]
    assert len(find_candidates(tools)["get_issue"]) == 2  # ambiguous: a human must choose


# --------------------------------------------------------------------------- #
# Mapping validation (metadata unit tests)
# --------------------------------------------------------------------------- #


def test_valid_mapping_is_schema_verified():
    tool = _tool()
    check = validate_mapping(_issue_mapping(tool), [tool])
    assert check.ok, check.problems
    assert check.warnings == ()


def test_mapping_to_unlisted_tool_is_rejected():
    tool = _tool()
    check = validate_mapping(_issue_mapping(tool, tool="fixture_srv.get_issue"), [tool])
    assert not check.ok
    assert "not in the tool list" in check.problems[0]


def test_mapping_name_must_match_exactly():
    tool = _tool("fixture_srv.issue_read")
    for near_miss in ("fixture_srv_issue_read", "issue_read", "FIXTURE_SRV.ISSUE_READ"):
        assert not validate_mapping(_issue_mapping(tool, tool=near_miss), [tool]).ok


def test_required_tool_argument_missing_from_mapping_is_rejected():
    tool = _tool()
    check = validate_mapping(_issue_mapping(tool, fixed_arguments={}), [tool])
    assert not check.ok
    assert any("'method' is neither mapped nor fixed" in p for p in check.problems)


def test_required_parameter_not_mapped_is_rejected():
    tool = _tool()
    check = validate_mapping(_issue_mapping(tool, arguments={"owner": "owner", "repo": "repo"}), [tool])
    assert any("'issue_number' is not mapped" in p for p in check.problems)


def test_wrong_parameter_type_is_rejected():
    schema = copy.deepcopy(ISSUE_SCHEMA)
    schema["properties"]["issue_number"] = {"type": "string"}
    tool = _tool(schema=schema)
    check = validate_mapping(_issue_mapping(tool), [tool])
    assert any("'issue_number' is integer, but tool argument 'issue_number' accepts string" in p for p in check.problems)


def test_argument_names_are_never_renamed_implicitly():
    schema = copy.deepcopy(ISSUE_SCHEMA)
    schema["properties"]["issueNumber"] = schema["properties"].pop("issue_number")
    schema["required"] = ["method", "owner", "repo", "issueNumber"]
    tool = _tool(schema=schema)
    check = validate_mapping(_issue_mapping(tool), [tool])
    assert any("'issue_number', which is not in the tool's inputSchema" in p for p in check.problems)
    # An explicit, reviewed mapping to the real name is accepted.
    explicit = _issue_mapping(tool, arguments={"owner": "owner", "repo": "repo", "issue_number": "issueNumber"})
    assert validate_mapping(explicit, [tool]).ok


def test_fixed_argument_must_satisfy_its_schema():
    tool = _tool()
    check = validate_mapping(_issue_mapping(tool, fixed_arguments={"method": "delete"}), [tool])
    assert any("fixed argument 'method' does not match its schema" in p for p in check.problems)


def test_changed_schema_invalidates_a_reviewed_mapping():
    tool = _tool()
    mapping = _issue_mapping(tool)
    schema = copy.deepcopy(ISSUE_SCHEMA)
    schema["properties"]["owner"]["description"] = "changed upstream"
    check = validate_mapping(mapping, [_tool(schema=schema)])
    assert any("inputSchema changed since review" in p for p in check.problems)


@pytest.mark.parametrize(
    "target",
    ["_task_id", "_delegation_id", "__agntid_x", "agntid_task_id", "agntid_delegation_id", "agntid_approval"],
)
def test_mapping_to_reserved_correlation_argument_is_rejected(target):
    schema = copy.deepcopy(ISSUE_SCHEMA)
    schema["properties"][target] = {"type": "string"}
    tool = _tool(schema=schema)
    mapping = _issue_mapping(tool, arguments={"owner": target, "repo": "repo", "issue_number": "issue_number"})
    assert any("reserved AgntID argument" in p for p in validate_mapping(mapping, [tool]).problems)


def test_write_tools_and_control_tools_cannot_back_a_read_capability():
    writer = _tool(annotations={"readOnlyHint": False})
    assert any("not read-only" in p for p in validate_mapping(_issue_mapping(writer), [writer]).problems)
    control = _tool(agntid.TASK_CLOSE_TOOL)
    assert any("control tool" in p for p in validate_mapping(_issue_mapping(control), [control]).problems)


def test_undeclared_read_only_hint_is_a_warning_not_a_pass_condition():
    tool = _tool(annotations={})
    check = validate_mapping(_issue_mapping(tool), [tool])
    assert check.ok and "readOnlyHint" in check.warnings[0]


def test_untyped_argument_cannot_be_verified():
    schema = copy.deepcopy(ISSUE_SCHEMA)
    schema["properties"]["owner"] = {"description": "anything"}
    tool = _tool(schema=schema)
    assert any("declares no type" in p for p in validate_mapping(_issue_mapping(tool), [tool]).problems)


def test_required_tool_argument_from_optional_parameter_is_rejected():
    schema = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in ("owner", "repo", "path", "ref")},
        "required": ["owner", "repo", "path", "ref"],
    }
    tool = _tool("fixture_srv.get_file_contents", schema=schema)
    mapping = CapabilityMapping(
        "read_file", tool.name, schema_fingerprint(schema),
        {"owner": "owner", "repo": "repo", "path": "path", "ref": "ref"},
    )
    assert any("mapped from an optional parameter" in p for p in validate_mapping(mapping, [tool]).problems)


# --------------------------------------------------------------------------- #
# Capability map file and arguments (unit tests)
# --------------------------------------------------------------------------- #

DIGEST = "0" * 64


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"capabilities": {"push_code": {"tool": "x", "schema_sha256": DIGEST, "arguments": {}}}},
        {"capabilities": {"get_issue": {"tool": "x", "arguments": {}}}},  # no schema pin
        {"capabilities": {"get_issue": {"tool": "x", "schema_sha256": "abc", "arguments": {}}}},
        {"capabilities": {"get_issue": {"tool": "", "schema_sha256": DIGEST, "arguments": {}}}},
        {"capabilities": {"get_issue": {"tool": "x", "schema_sha256": DIGEST, "arguments": {"user": "u"}}}},
        {"capabilities": {"get_issue": {"tool": "x", "schema_sha256": DIGEST, "arguments": {}, "retry": 3}}},
    ],
)
def test_malformed_capability_maps_are_config_errors(data):
    with pytest.raises(ConfigError):
        parse_capability_map(data, source="test")


def test_capability_map_file_round_trip(tmp_path):
    path = tmp_path / "map.json"
    entry = {"tool": "fixture_srv.issue_read", "schema_sha256": DIGEST, "arguments": {"owner": "owner"}}
    path.write_text(json.dumps({"capabilities": {"get_issue": entry}}))
    [mapping] = load_capability_map(path).values()
    assert (mapping.tool, mapping.source) == ("fixture_srv.issue_read", str(path))
    with pytest.raises(ConfigError, match="not valid JSON"):
        (tmp_path / "bad.json").write_text("{")
        load_capability_map(tmp_path / "bad.json")


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"owner": "o", "repo": "r"}, "missing required parameter 'issue_number'"),
        ({"owner": "o", "repo": "r", "issue_number": "7"}, "must be an integer"),
        ({"owner": "o", "repo": "r", "issue_number": True}, "must be an integer"),
        ({"owner": "", "repo": "r", "issue_number": 7}, "non-empty string"),
        ({"owner": "o", "repo": "r", "issue_number": 7, "state": "open"}, "unknown parameter 'state'"),
    ],
)
def test_bad_parameters_are_rejected_without_coercion(params, expected):
    tool = _tool()
    arguments, problems = build_tool_arguments(_issue_mapping(tool), tool, params)
    assert arguments is None
    assert any(expected in p for p in problems)


def test_arguments_are_built_exactly_and_checked_against_the_schema():
    tool = _tool()
    arguments, problems = build_tool_arguments(
        _issue_mapping(tool), tool, {"owner": "o", "repo": "r", "issue_number": 7}
    )
    assert problems == ()
    assert arguments == {"method": "get", "owner": "o", "repo": "r", "issue_number": 7}


def test_schema_constraints_beyond_type_are_enforced_before_sending():
    schema = copy.deepcopy(ISSUE_SCHEMA)
    schema["properties"]["issue_number"] = {"type": "integer", "minimum": 1}
    tool = _tool(schema=schema)
    arguments, problems = build_tool_arguments(
        _issue_mapping(tool), tool, {"owner": "o", "repo": "r", "issue_number": 0}
    )
    assert arguments is None
    assert problems == ("tool inputSchema: 0 is less than the minimum of 1",)


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("ToolError", OperationStatus.EXECUTION_FAILED),  # the server answered with an error
        ("McpError", OperationStatus.OUTCOME_UNKNOWN),  # includes JSON-RPC request timeouts
        ("ReadTimeout", OperationStatus.OUTCOME_UNKNOWN),
        ("RemoteProtocolError", OperationStatus.OUTCOME_UNKNOWN),
        ("NoneType", OperationStatus.OUTCOME_UNKNOWN),  # unrecognized result shape
    ],
)
def test_only_a_server_error_answer_is_a_definite_failure(error_type, expected):
    # A Module 2 result value built directly: the status mapping is a pure function.
    call = ToolCallResult(status=CallStatus.ERROR, tool_name="t", task_id="task", error_type=error_type)
    assert operation_status(call) is expected


# --------------------------------------------------------------------------- #
# SDK transport tests (fixture server: NOT AgntID, NOT GitHub)
# --------------------------------------------------------------------------- #


def _config(server, timeout=10.0):
    return ConnectorConfig(server.url, timeout_seconds=timeout)


async def _mappings(server) -> dict[str, CapabilityMapping]:
    """Mappings written the way a reviewer would: from the discovered metadata."""
    tools = {t.name: t for t in (await discover_tools(_config(server))).tools}

    def digest(name):
        return schema_fingerprint(tools[name].input_schema)

    owner_repo = {"owner": "owner", "repo": "repo"}
    return {
        "get_issue": CapabilityMapping(
            "get_issue", "fixture_github_issue_read", digest("fixture_github_issue_read"),
            {**owner_repo, "issue_number": "issue_number"}, {"method": "get"},
        ),
        "get_repository": CapabilityMapping(
            "get_repository", "fixture_github_get_repository", digest("fixture_github_get_repository"), owner_repo,
        ),
        "read_file": CapabilityMapping(
            "read_file", "fixture_github_get_file_contents", digest("fixture_github_get_file_contents"),
            {**owner_repo, "path": "path", "ref": "ref"},
        ),
    }


ISSUE_PARAMS = {"owner": "fixture-org", "repo": "fixture-repo", "issue_number": 7}


async def test_discovery_preserves_exact_names_and_schemas(github_task_server):
    result = await discover_tools(_config(github_task_server))
    async with fastmcp.Client(github_task_server.url) as raw_client:
        raw = {t.name: t.inputSchema for t in await raw_client.list_tools()}
    assert {t.name: t.input_schema for t in result.tools} == raw
    candidates = find_candidates(result.business_tools)
    assert [t.name for t in candidates["get_issue"]] == ["fixture_github_issue_read"]
    assert "fixture_github_create_issue" not in {t.name for ts in candidates.values() for t in ts}


async def test_verified_reads_use_the_wrapped_dispatcher_with_exact_arguments(github_task_server):
    server = github_task_server
    server.task_open_response = fixture_accepted(
        "fixture_github_issue_read", "fixture_github_get_repository", "fixture_github_get_file_contents"
    )
    mappings = await _mappings(server)
    async with agntid.AgntidMCPClient(server.url) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            issue = await execute_capability(session, mappings, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
            repo = await execute_capability(
                session, mappings, "get_repository", {"owner": "fixture-org", "repo": "fixture-repo"}, timeout_seconds=10
            )
            file = await execute_capability(
                session, mappings, "read_file",
                {"owner": "fixture-org", "repo": "fixture-repo", "path": "README.md", "ref": "main"},
                timeout_seconds=10,
            )
    assert issue.ok and issue.value == {
        "note": FIXTURE_NOTE, "repo": "fixture-org/fixture-repo", "number": 7, "method": "get"
    }
    assert repo.ok and repo.value["full_name"] == "fixture-org/fixture-repo"
    assert file.ok and file.value == {"result": f"{FIXTURE_NOTE}: fixture-org/fixture-repo/README.md@main"}
    assert issue.provenance["taskId"] == session.task_id
    assert issue.provenance["toolName"] == "fixture_github_issue_read"
    # agntid_task_id on the wire is only ever added by agntid.wrap_client.
    assert server.business_calls()[0] == (
        "fixture_github_issue_read",
        {"method": "get", "owner": "fixture-org", "repo": "fixture-repo", "issue_number": 7, "agntid_task_id": session.task_id},
    )
    assert len(server.business_calls()) == 3


async def test_visible_tool_excluded_from_task_list_is_not_called(github_task_server):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_get_repository")
    mappings = await _mappings(server)
    async with agntid.AgntidMCPClient(server.url) as client:
        visible = {t.name for t in await agntid.get_tools_list(client)}
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await execute_capability(session, mappings, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
    assert "fixture_github_issue_read" in visible
    assert result.status is OperationStatus.NOT_IN_TASK and not result.call_sent
    assert server.business_calls() == []


async def test_empty_task_list_fails_closed_for_every_capability(github_task_server):
    server = github_task_server
    server.task_open_response = fixture_accepted()
    mappings = await _mappings(server)
    async with agntid.AgntidMCPClient(server.url) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            for cap, params in (("get_issue", ISSUE_PARAMS), ("get_repository", {"owner": "o", "repo": "r"})):
                result = await execute_capability(session, mappings, cap, params, timeout_seconds=10)
                assert result.status is OperationStatus.NOT_IN_TASK
    assert server.business_calls() == []


async def test_denied_task_prevents_any_read(github_task_server):
    server = github_task_server
    server.task_open_response = {"ok": False, "reason": "fixture: task rejected"}
    mappings = await _mappings(server)
    async with agntid.AgntidMCPClient(server.url) as client:
        with pytest.raises(TaskOpenError, match="fixture: task rejected"):
            async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
                await execute_capability(session, mappings, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
    assert server.business_calls() == []


async def test_missing_capability_and_invalid_mapping_send_nothing(github_task_server):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read")
    mappings = await _mappings(server)
    stale = {"get_issue": CapabilityMapping(**{**mappings["get_issue"].__dict__, "schema_sha256": "f" * 64})}
    async with agntid.AgntidMCPClient(server.url) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            missing = await execute_capability(session, {}, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
            invalid = await execute_capability(session, stale, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
            bad_args = await execute_capability(
                session, mappings, "get_issue", {**ISSUE_PARAMS, "issue_number": "7"}, timeout_seconds=10
            )
    assert missing.status is OperationStatus.MISSING_CAPABILITY
    assert invalid.status is OperationStatus.INVALID_MAPPING
    assert bad_args.status is OperationStatus.INVALID_ARGUMENTS
    assert not any(r.call_sent for r in (missing, invalid, bad_args))
    assert server.business_calls() == []


async def test_denial_is_propagated_once_without_retry(github_task_server):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read", "fixture_github_get_repository")
    server.denials["fixture_github_issue_read"] = fixture_denial("fixture: repository not allowed", "fixture-repo-scope")
    mappings = await _mappings(server)
    async with agntid.AgntidMCPClient(server.url) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await execute_capability(session, mappings, "get_issue", ISSUE_PARAMS, timeout_seconds=10)
    assert result.status is OperationStatus.DENIED
    assert result.denial["policy_name"] == "fixture-repo-scope"
    assert "fixture: repository not allowed" in result.message
    assert [name for name, _ in server.business_calls()] == ["fixture_github_issue_read"]


async def test_timeout_is_outcome_unknown_and_never_retried(github_task_server):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read")
    server.delays["fixture_github_issue_read"] = 3.0
    mappings = await _mappings(server)
    async with agntid.AgntidMCPClient(server.url) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await execute_capability(session, mappings, "get_issue", ISSUE_PARAMS, timeout_seconds=0.5)
            assert session.active
    assert result.status is OperationStatus.OUTCOME_UNKNOWN
    assert result.call_sent and "not retried" in result.message
    assert len(server.called("fixture_github_issue_read")) == 1
    assert len(server.called("agntid_task_close")) == 1  # task still closed cleanly


async def test_server_side_tool_error_is_execution_failed_not_retried(github_task_server):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_broken_read")
    tools = {t.name: t for t in (await discover_tools(_config(server))).tools}
    broken = tools["fixture_github_broken_read"]
    mappings = {
        "get_repository": CapabilityMapping(
            "get_repository", broken.name, schema_fingerprint(broken.input_schema), {"owner": "owner", "repo": "repo"}
        )
    }
    async with agntid.AgntidMCPClient(server.url) as client:
        async with open_task_session(client, _config(server), IDENTITY, PROMPT) as session:
            result = await execute_capability(
                session, mappings, "get_repository", {"owner": "o", "repo": "r"}, timeout_seconds=10
            )
    assert result.status is OperationStatus.EXECUTION_FAILED
    assert "fixture backend failure" in result.message
    assert len(server.called("fixture_github_broken_read")) == 1
