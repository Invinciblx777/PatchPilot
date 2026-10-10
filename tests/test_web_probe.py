"""Task probe runs (POST /api/runs kind=probe).

The runtime is the task fixture server from conftest.py: NOT AgntID. Its
shortlists and denials are fixture payloads. These tests show that the probe
opens a real SDK task, calls at most one tool through the Module 2 dispatcher,
refuses what Module 3's read-only rule refuses, and reports exactly what came
back. They show nothing about a live AgntID runtime.
"""

from __future__ import annotations

import pytest

from .conftest import fixture_accepted, fixture_denial
from .web_helpers import agntid_env, last_event, make_settings, serve_web, statuses

PROMPT = "Add 11 and 22"


def _web(server, **env):
    return serve_web(make_settings(agntid_env(server.url, **env)))


def _call(tool, arguments, confirm=False):
    return {"tool": tool, "arguments": arguments, "confirmUnannotated": confirm}


def test_shortlist_only_probe_opens_and_closes_a_task_without_calls(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file", "fixture_add_numbers")
    with _web(task_server) as web:
        run = web.run("probe", prompt=PROMPT)
    assert run["outcome"] == "task_opened"
    assert statuses(run) == [("runtime", "ok"), ("task", "ok"), ("task", "info")]
    task = [e for e in run["events"] if e["step"] == "task"][0]
    assert task["source"] == "agntid"
    assert task["detail"]["shortlist"] == ["fixture_add_numbers", "fixture_read_file"]
    assert [name for name, _ in task_server.calls] == ["agntid_task_open", "agntid_task_close"]


def test_confirmed_call_is_sent_once_through_the_task_and_allowed(task_server):
    task_server.task_open_response = fixture_accepted("fixture_add_numbers")
    with _web(task_server) as web:
        run = web.run("probe", prompt=PROMPT, call=_call("fixture_add_numbers", {"first_number": 11, "second_number": 22}, True))
    assert run["outcome"] == "success"
    call = last_event(run, "call")
    assert (call["source"], call["status"]) == ("agntid", "ok")
    assert call["detail"]["decision"] == "allowed" and call["detail"]["execution"] == "success"
    assert call["detail"]["value"] == {"result": 33.0}
    task_id = [e for e in run["events"] if e["step"] == "task"][0]["detail"]["taskId"]
    assert task_server.business_calls() == [
        ("fixture_add_numbers", {"first_number": 11, "second_number": 22, "agntid_task_id": task_id})
    ]
    assert len(task_server.called("agntid_task_close")) == 1


@pytest.mark.parametrize(
    ("call", "problem"),
    [
        (_call("fixture_add_numbers", {"first_number": 1, "second_number": 2}), "readOnlyHint"),
        (_call("agntid_task_open", {}, True), "control tool"),
        (_call("fixture_not_listed", {}, True), "not listed"),
        (_call("fixture_add_numbers", {"first_number": "eleven", "second_number": 2}, True), "inputSchema"),
        (_call("fixture_add_numbers", {"first_number": 1, "second_number": 2, "_task_id": "x"}, True), "reserved"),
        # The live runtime's schemas carry agntid_task_id / agntid_delegation_id /
        # agntid_approval; a browser must never be able to fill them.
        (_call("fixture_add_numbers", {"first_number": 1, "second_number": 2, "agntid_approval": "ok"}, True), "runtime-managed"),
        (_call("fixture_add_numbers", {"first_number": 1, "second_number": 2, "AgntID_Task_Id": "t"}, True), "runtime-managed"),
    ],
)
def test_refused_calls_send_nothing_and_open_no_task(task_server, call, problem):
    task_server.task_open_response = fixture_accepted("fixture_add_numbers")
    with _web(task_server) as web:
        run = web.run("probe", prompt=PROMPT, call=call)
    assert run["outcome"] == "refused"
    refusal = last_event(run, "probe_check")
    assert (refusal["source"], refusal["status"]) == ("patchpilot", "error")
    assert any(problem in p for p in refusal["detail"]["problems"]), refusal["detail"]["problems"]
    assert task_server.calls == []


def test_tools_that_declare_writes_are_refused_even_when_confirmed(github_task_server):
    with _web(github_task_server) as web:
        run = web.run("probe", prompt="Create an issue", call=_call(
            "fixture_github_create_issue", {"owner": "o", "repo": "r", "title": "t"}, True))
    assert run["outcome"] == "refused"
    assert any("not read-only" in p for p in last_event(run, "probe_check")["detail"]["problems"])
    assert github_task_server.calls == []


def test_tool_declaring_read_only_needs_no_confirmation(github_task_server):
    github_task_server.task_open_response = fixture_accepted("fixture_github_get_repository")
    with _web(github_task_server) as web:
        run = web.run("probe", prompt="Read repository o/r", call=_call(
            "fixture_github_get_repository", {"owner": "o", "repo": "r"}))
    assert run["outcome"] == "success"
    assert last_event(run, "probe_check")["detail"]["readOnly"] == "declared"


def test_denial_is_shown_with_its_reason_and_policy(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file")
    task_server.denials["fixture_read_file"] = fixture_denial("fixture: path not allowed", "fixture-path-scope")
    with _web(task_server) as web:
        run = web.run("probe", prompt="Read README.md", call=_call("fixture_read_file", {"path": "README.md"}, True))
    call = last_event(run, "call")
    assert run["outcome"] == "denied"
    assert (call["source"], call["status"]) == ("agntid", "denied")
    assert call["detail"]["decision"] == "denied" and call["detail"]["execution"] == "blocked"
    assert call["detail"]["denial"]["reason"] == "fixture: path not allowed"
    assert call["detail"]["denial"]["policy_name"] == "fixture-path-scope"
    assert len(task_server.called("fixture_read_file")) == 1


def test_review_required_is_not_reported_as_a_plain_denial(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file")
    task_server.denials["fixture_read_file"] = {
        "__agntid_error": "Request held for review",
        "__agntid_policy_decision": {
            "allowed": False,
            "reason": "fixture: needs approval",
            "policy_name": "fixture-review",
            "intent_validation": {"verdict": "review_required",
                                  "review": {"review_id": "rev-fixture-1", "status": "pending",
                                             "outcome": "review_required"}},
        },
    }
    with _web(task_server) as web:
        run = web.run("probe", prompt="Read README.md", call=_call("fixture_read_file", {"path": "README.md"}, True))
    call = last_event(run, "call")
    assert call["status"] == "denied" and call["detail"]["decision"] == "review"
    assert call["detail"]["denial"]["review"]["review_id"] == "rev-fixture-1"
    assert call["detail"]["denial"]["review"]["status"] == "pending"


def test_tool_outside_the_shortlist_is_rejected_by_the_dispatcher(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file")
    with _web(task_server) as web:
        run = web.run("probe", prompt=PROMPT, call=_call("fixture_add_numbers", {"first_number": 1, "second_number": 2}, True))
    call = last_event(run, "call")
    assert run["outcome"] == "rejected"
    assert (call["source"], call["status"]) == ("patchpilot", "error")
    assert call["detail"]["callSent"] is False and call["detail"]["decision"] == "not_observed"
    assert task_server.business_calls() == []


def test_tool_error_is_a_failure_with_no_decision_claimed(task_server):
    task_server.task_open_response = fixture_accepted("fixture_explode")
    with _web(task_server) as web:
        run = web.run("probe", prompt="Explode", call=_call("fixture_explode", {}, True))
    call = last_event(run, "call")
    assert run["outcome"] == "execution_failed"
    assert call["status"] == "error"
    assert call["detail"]["decision"] == "not_determined" and call["detail"]["execution"] == "failed"
    assert "fixture backend failure" in call["detail"]["message"]


def test_timeout_is_outcome_unknown_and_the_task_still_closes(task_server):
    task_server.task_open_response = fixture_accepted("fixture_read_file")
    task_server.delays["fixture_read_file"] = 3.0
    with _web(task_server, AGNTID_TIMEOUT_SECONDS="1") as web:
        run = web.run("probe", prompt="Read README.md", call=_call("fixture_read_file", {"path": "README.md"}, True))
    call = last_event(run, "call")
    assert run["outcome"] == "outcome_unknown"
    assert call["detail"]["decision"] == "unknown" and call["detail"]["execution"] == "unknown"
    assert len(task_server.called("fixture_read_file")) == 1
    assert len(task_server.called("agntid_task_close")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": ""},
        {"prompt": PROMPT, "call": {"tool": "fixture_read_file", "arguments": [], "confirmUnannotated": True}},
        {"prompt": PROMPT, "call": {"tool": "", "arguments": {}, "confirmUnannotated": True}},
        {"prompt": PROMPT, "call": {"tool": "fixture_read_file", "arguments": {}, "confirm": True}},
        {"prompt": PROMPT, "extra": 1},
    ],
)
def test_malformed_probe_requests_are_rejected(task_server, body):
    with _web(task_server) as web:
        response = web.start("probe", **body)
    assert response.status_code == 400
    assert task_server.calls == []
