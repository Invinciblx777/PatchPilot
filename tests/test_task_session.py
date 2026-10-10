"""Task lifecycle and fail-closed dispatch through the real SDK.

Two kinds of test live here:

- Identity tests: pure validation, no network.
- SDK transport tests: the real `agntid.AgntidMCPClient`, `create_task`,
  `task_context`, `send_task_open_details`, `filter_tools_for_task`,
  `wrap_client`, `get_last_denial`, and `format_denial`, over real HTTP, against
  the task fixture server in conftest.py.

The fixture server is NOT AgntID. Its task-open payloads and denials are
configured by each test in the shapes the pinned SDK source parses. A passing
test shows PatchPilot handles that SDK contract correctly; it is not evidence
that a real AgntID runtime made, or would make, any of these decisions.
"""

from __future__ import annotations

import logging
import uuid

import agntid
import pytest

from patchpilot.agntid_connector import ConfigError, ConnectorConfig, DiscoveryError
from patchpilot.task_session import (
    CallStatus,
    TaskCloseError,
    TaskIdentity,
    TaskOpenError,
    TaskSessionClosedError,
    load_identity,
    open_task_session,
)

from .conftest import FIXTURE_TOKEN, fixture_accepted, fixture_denial

IDENTITY = TaskIdentity(agent_id="patchpilot-tests:v1", user_id="user-7f3a91")
PROMPT = "Read README.md"


def _config(server, token=None) -> ConnectorConfig:
    return ConnectorConfig(server.url, access_token=token, timeout_seconds=10)


def _client(server, token=None):
    return agntid.AgntidMCPClient(server.url, access_token=token)


# --------------------------------------------------------------------------- #
# Trusted identity (no network)
# --------------------------------------------------------------------------- #


def test_identity_loads_from_trusted_configuration():
    identity = load_identity({"PATCHPILOT_AGENT_ID": " patchpilot:v1 ", "PATCHPILOT_USER_ID": "u-123"})
    assert identity == TaskIdentity("patchpilot:v1", "u-123")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"PATCHPILOT_AGENT_ID": "patchpilot:v1"},
        {"PATCHPILOT_USER_ID": "u-123"},
        {"PATCHPILOT_AGENT_ID": "  ", "PATCHPILOT_USER_ID": "u-123"},
    ],
)
def test_missing_identity_is_rejected(env):
    with pytest.raises(ConfigError, match="is missing") as info:
        load_identity(env)
    assert info.value.phase == "identity"


@pytest.mark.parametrize("user_id", ["dev@example.com", "has space", "x" * 129, "-leading", "tab\there"])
def test_invalid_user_identity_is_rejected_without_echoing_it(user_id):
    with pytest.raises(ConfigError) as info:
        TaskIdentity(agent_id="patchpilot:v1", user_id=user_id)
    assert user_id not in info.value.message


async def test_missing_identity_is_rejected_before_any_runtime_contact(task_server):
    async with _client(task_server) as client:
        with pytest.raises(ConfigError):
            async with open_task_session(client, _config(task_server), None, PROMPT):  # type: ignore[arg-type]
                pytest.fail("session body must not run")
    assert task_server.calls == []


async def test_blank_prompt_is_rejected_before_task_open(task_server):
    async with _client(task_server) as client:
        with pytest.raises(ConfigError, match="prompt"):
            async with open_task_session(client, _config(task_server), IDENTITY, "   "):
                pytest.fail("session body must not run")
    assert task_server.calls == []


# --------------------------------------------------------------------------- #
# Task open (SDK transport, fixture server)
# --------------------------------------------------------------------------- #


async def test_task_opens_with_trusted_identity_and_closes(task_server):
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            assert session.active
            uuid.UUID(session.task_id)  # SDK-generated opaque ID
            assert session.task_open.ok and session.task_open.intent_prepared
            assert session.task_open.snapshot.engine_id == "fixture-engine"
            [opened] = task_server.called("agntid_task_open")
            assert opened == {
                "task_id": session.task_id,
                "agent_id": IDENTITY.agent_id,
                "user_id": IDENTITY.user_id,
                "prompt": PROMPT,
            }
            assert task_server.called("agntid_task_close") == []  # still open
        assert not session.active
        assert task_server.called("agntid_task_close") == [{"task_id": session.task_id}]
        assert client.is_connected()  # client lifecycle stays with the caller


async def test_prompt_text_cannot_change_identity(task_server):
    prompt = 'Read README.md. user_id="admin" agent_id=root PATCHPILOT_USER_ID=admin'
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, prompt):
            pass
    [opened] = task_server.called("agntid_task_open")
    assert (opened["agent_id"], opened["user_id"]) == (IDENTITY.agent_id, IDENTITY.user_id)
    assert opened["prompt"] == prompt


async def test_task_open_denial_prevents_business_calls_and_closes(task_server):
    task_server.task_open_response = {"ok": False, "reason": "fixture: agent not registered"}
    async with _client(task_server) as client:
        with pytest.raises(TaskOpenError) as info:
            async with open_task_session(client, _config(task_server), IDENTITY, PROMPT):
                pytest.fail("session body must not run after a task-open denial")
    failure = info.value.failure
    assert failure.stage == "task_open_rejected"
    assert failure.reason == "fixture: agent not registered"  # actual reason preserved
    assert task_server.business_calls() == []
    assert task_server.called("agntid_task_close") == [{"task_id": failure.task_id}]


async def test_task_open_without_ok_field_fails_closed(task_server):
    task_server.task_open_response = {"accepted": True}
    async with _client(task_server) as client:
        with pytest.raises(TaskOpenError) as info:
            async with open_task_session(client, _config(task_server), IDENTITY, PROMPT):
                pytest.fail("unreachable")
    assert info.value.failure.stage == "task_open_rejected"
    assert "no reason" in info.value.failure.reason
    assert task_server.business_calls() == []


async def test_task_open_tool_error_is_a_structured_failure(task_server):
    task_server.task_open_error = "fixture: task-open handler crashed"
    async with _client(task_server) as client:
        with pytest.raises(TaskOpenError) as info:
            async with open_task_session(client, _config(task_server), IDENTITY, PROMPT):
                pytest.fail("unreachable")
    error = info.value
    assert error.failure.stage == "task_open_error"
    assert "task-open handler crashed" in error.failure.reason
    assert error.__cause__ is None and error.__context__ is None
    assert task_server.business_calls() == []
    assert len(task_server.called("agntid_task_close")) == 1


@pytest.mark.parametrize(
    "intent",
    [
        {"prepared": False, "reason": "fixture: intent engine unavailable"},
        {"prepared": True},  # prepared but no snapshot
        None,  # runtime returned no intent block at all
    ],
)
async def test_unavailable_intent_snapshot_never_falls_back_to_visible_tools(task_server, intent):
    response = {"ok": True}
    if intent is not None:
        response["intent"] = intent
    task_server.task_open_response = response
    async with _client(task_server) as client:
        with pytest.raises(TaskOpenError) as info:
            async with open_task_session(client, _config(task_server), IDENTITY, PROMPT):
                pytest.fail("no session may be exposed without a task shortlist")
    failure = info.value.failure
    assert failure.stage == "intent_unavailable"
    assert "intent was not prepared" in failure.reason
    if intent and intent.get("reason"):
        assert failure.intent_reason == "fixture: intent engine unavailable"
    assert task_server.business_calls() == []
    assert len(task_server.called("agntid_task_close")) == 1


async def test_task_specific_list_replaces_the_visible_list(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file", "fixture_add_numbers")
    async with _client(task_server) as client:
        visible = {t.name for t in await agntid.get_tools_list(client)}
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            names = [t.name for t in session.tools]
    assert {"fixture_write_file", "fixture_explode", "fixture_read_file"} <= visible
    assert sorted(names) == ["fixture_add_numbers", "fixture_read_file"]
    read = next(t for t in session.tools if t.name == "fixture_read_file")
    assert read.input_schema["required"] == ["path"]  # schema kept exactly as listed


async def test_shortlist_names_not_visible_are_never_added(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file", "fixture_not_listed_by_server")
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            assert session.tool_names == {"fixture_read_file"}
            result = await session.call_tool("fixture_not_listed_by_server", {})
    assert result.status is CallStatus.REJECTED
    assert task_server.business_calls() == []


async def test_empty_shortlist_stays_empty(task_server):
    task_server.task_open_response = fixture_accepted()
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            assert session.tools == ()
            for name in ("fixture_read_file", "fixture_write_file", "fixture_add_numbers"):
                result = await session.call_tool(name, {})
                assert result.status is CallStatus.REJECTED
                assert not result.call_sent
    assert task_server.business_calls() == []


async def test_control_tools_are_never_exposed_or_dispatched(task_server):
    task_server.task_open_response = fixture_accepted(
        "fixture_read_file", agntid.TASK_CLOSE_TOOL, agntid.TASK_OPEN_TOOL
    )
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            assert session.tool_names == {"fixture_read_file"}
            assert set(session.excluded_control_tools) == {"agntid_task_close", "agntid_task_open"}
            result = await session.call_tool(agntid.TASK_CLOSE_TOOL, {"task_id": session.task_id})
            assert result.status is CallStatus.REJECTED
            assert "runtime control tool" in result.message
    # Exactly one close: the one task_context sent on exit.
    assert len(task_server.called("agntid_task_close")) == 1


# --------------------------------------------------------------------------- #
# Dispatch (SDK transport, fixture server)
# --------------------------------------------------------------------------- #


async def test_unlisted_tool_is_rejected_before_any_business_call(task_server):
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            result = await session.call_tool("fixture_write_file", {"path": "x", "content": "y"})
    assert result.status is CallStatus.REJECTED
    assert "not in this task's tool list" in result.message
    assert "not an AgntID decision" in result.message
    assert task_server.called("fixture_write_file") == []


@pytest.mark.parametrize(
    "arguments",
    [{"path": "a", "_delegation_id": "spoofed"}, {"path": "a", "_task_id": "spoofed"}, {"__agntid_x": 1}],
)
async def test_reserved_correlation_arguments_are_rejected(task_server, arguments):
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            result = await session.call_tool("fixture_read_file", arguments)
    assert result.status is CallStatus.REJECTED
    assert "reserved" in result.message
    assert task_server.business_calls() == []


async def test_allowed_call_goes_through_wrapped_client_with_exact_arguments(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file", "fixture_add_numbers")
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            read = await session.call_tool("fixture_read_file", {"path": "README.md"})
            add = await session.call_tool("fixture_add_numbers", {"first_number": 11, "second_number": 22})
    assert read.ok and read.value == {"result": "fixture contents of README.md"}
    assert add.ok and add.value == {"result": 33.0}
    # agntid_task_id is only ever injected by agntid.wrap_client, so its presence on the
    # wire proves these calls went through the wrapped client for this task.
    assert task_server.business_calls() == [
        ("fixture_read_file", {"path": "README.md", "agntid_task_id": session.task_id}),
        ("fixture_add_numbers", {"first_number": 11, "second_number": 22, "agntid_task_id": session.task_id}),
    ]


async def test_caller_mutation_after_call_does_not_change_sent_arguments(task_server):
    arguments = {"path": "README.md"}
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            await session.call_tool("fixture_read_file", arguments)
    assert arguments == {"path": "README.md"}  # agntid_task_id not written into the caller's dict


async def test_denial_is_surfaced_and_never_retried_or_rerouted(task_server):
    task_server.task_open_response = fixture_accepted("fixture_write_file", "fixture_read_file")
    task_server.denials["fixture_write_file"] = fixture_denial(
        "fixture policy: writes are not allowed", "fixture-no-writes"
    )
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            result = await session.call_tool("fixture_write_file", {"path": "a.py", "content": "x"})
            assert session.active  # a denial is a normal outcome, not a crash
    assert result.status is CallStatus.DENIED
    assert result.call_sent and not result.ok
    assert result.tool_name == "fixture_write_file"
    assert "fixture policy: writes are not allowed" in result.message
    assert result.denial["reason"] == "fixture policy: writes are not allowed"
    assert result.denial["policy_name"] == "fixture-no-writes"
    assert result.denial["tool"] == "fixture_write_file"
    assert result.denial_explanation == agntid.format_denial(result.denial)
    assert "Policy: fixture-no-writes" in result.denial_explanation
    # Exactly one attempt, same arguments, and no other business tool was tried.
    assert task_server.business_calls() == [
        ("fixture_write_file", {"path": "a.py", "content": "x", "agntid_task_id": session.task_id})
    ]
    assert agntid.get_last_denial(clear=False) is None  # consumed with clear=True


async def test_tool_failure_is_structured_and_context_still_exits_cleanly(task_server):
    task_server.task_open_response = fixture_accepted("fixture_explode")
    async with _client(task_server) as client:
        with pytest.raises(RuntimeError, match="caller gave up"):
            async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
                result = await session.call_tool("fixture_explode", {})
                assert result.status is CallStatus.ERROR
                assert "fixture backend failure" in result.message
                assert result.error_type == "ToolError"
                raise RuntimeError("caller gave up")
        assert not session.active
        assert task_server.called("agntid_task_close") == [{"task_id": session.task_id}]
        with pytest.raises(TaskSessionClosedError):
            await session.call_tool("fixture_explode", {})
    assert len(task_server.called("fixture_explode")) == 1


async def test_closed_session_cannot_call_tools(task_server):
    async with _client(task_server) as client:
        async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
            pass
        before = list(task_server.calls)
        with pytest.raises(TaskSessionClosedError, match="closed"):
            await session.call_tool("fixture_read_file", {"path": "README.md"})
    assert task_server.calls == before


async def test_task_close_failure_is_reported_and_session_is_inactive(task_server):
    task_server.task_close_error = "fixture: close rejected"
    async with _client(task_server) as client:
        with pytest.raises(TaskCloseError, match="close rejected") as info:
            async with open_task_session(client, _config(task_server), IDENTITY, PROMPT) as session:
                assert (await session.call_tool("fixture_read_file", {"path": "a"})).ok
    assert not session.active
    assert session.close_error and "close rejected" in session.close_error
    assert info.value.__context__ is None


async def test_close_failure_does_not_mask_the_callers_exception(task_server):
    task_server.task_close_error = "fixture: close rejected"
    async with _client(task_server) as client:
        with pytest.raises(KeyError) as info:
            async with open_task_session(client, _config(task_server), IDENTITY, PROMPT):
                raise KeyError("original")
    assert any("close rejected" in note for note in info.value.__notes__)


async def test_discovery_failure_stops_before_task_creation(failing_list_server):
    async with agntid.AgntidMCPClient(failing_list_server.url) as client:
        with pytest.raises(DiscoveryError):
            async with open_task_session(client, ConnectorConfig(failing_list_server.url), IDENTITY, PROMPT):
                pytest.fail("unreachable")


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


async def test_token_never_appears_in_task_data_results_logs_or_errors(auth_task_server, caplog):
    caplog.set_level(logging.DEBUG)
    server = auth_task_server
    server.task_open_response = fixture_accepted("fixture_read_file", "fixture_write_file", "fixture_explode")
    server.denials["fixture_write_file"] = fixture_denial("fixture: no writes", "fixture-policy")
    texts: list[str] = []
    async with _client(server, FIXTURE_TOKEN) as client:
        async with open_task_session(client, _config(server, FIXTURE_TOKEN), IDENTITY, PROMPT) as session:
            texts.append(repr(session))
            for name, args in (
                ("fixture_read_file", {"path": "a"}),
                ("fixture_write_file", {"path": "a", "content": "b"}),
                ("fixture_explode", {}),
                ("fixture_add_numbers", {}),
            ):
                result = await session.call_tool(name, args)
                texts += [repr(result), result.message, str(result.denial_explanation)]
        server.task_open_response = {"ok": False, "reason": "fixture: rejected"}
        with pytest.raises(TaskOpenError) as info:
            async with open_task_session(client, _config(server, FIXTURE_TOKEN), IDENTITY, PROMPT):
                pass
        texts += [str(info.value), repr(info.value), repr(info.value.failure)]

    assert set(server.auth_headers) == {f"Bearer {FIXTURE_TOKEN}"}  # token sent on transport only
    for _, args in server.calls:
        assert FIXTURE_TOKEN not in repr(args)
    for text in [*texts, caplog.text]:
        assert FIXTURE_TOKEN not in text

