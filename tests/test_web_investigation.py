"""Issue investigation runs (POST /api/runs kind=investigation).

The runtime is the task fixture server plus GitHub-shaped fixture tools from
conftest.py: NOT AgntID and NOT GitHub. Its shortlists and denials are fixture
payloads. These tests show that the web layer drives Modules 1-3 correctly and
reports exactly what came back; they show nothing about a live AgntID runtime.
"""

from __future__ import annotations

import hashlib
import json
import time

import fastmcp
import pytest

from .conftest import FIXTURE_NOTE, fixture_accepted, fixture_denial
from .web_helpers import agntid_env, last_event, make_settings, serve_web, statuses

ISSUE_URL = "https://github.com/fixture-org/fixture-repo/issues/7"
PROMPT = "Investigate GitHub issue fixture-org/fixture-repo#7"


def _sha(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


async def _write_map(server, path, *, stale: tuple[str, ...] = ()) -> str:
    """A capability map written the way a reviewer would: from the real tool list."""
    async with fastmcp.Client(server.url) as client:
        schemas = {t.name: t.inputSchema for t in await client.list_tools()}

    def digest(name, cap):
        return "0" * 64 if cap in stale else _sha(schemas[name])

    path.write_text(json.dumps({"capabilities": {
        "get_issue": {
            "tool": "fixture_github_issue_read",
            "schema_sha256": digest("fixture_github_issue_read", "get_issue"),
            "arguments": {"owner": "owner", "repo": "repo", "issue_number": "issue_number"},
            "fixed_arguments": {"method": "get"},
        },
        "read_file": {
            "tool": "fixture_github_get_file_contents",
            "schema_sha256": digest("fixture_github_get_file_contents", "read_file"),
            "arguments": {"owner": "owner", "repo": "repo", "path": "path", "ref": "ref"},
        },
    }}))
    return str(path)


async def _settings(server, tmp_path, **map_kwargs):
    mapping = await _write_map(server, tmp_path / "capabilities.json", **map_kwargs)
    return make_settings(agntid_env(server.url, PATCHPILOT_CAPABILITY_MAP=mapping))


async def test_issue_and_file_reads_go_through_one_task(github_task_server, tmp_path):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read", "fixture_github_get_file_contents")
    with serve_web(await _settings(server, tmp_path)) as web:
        run = web.run("investigation", issue=ISSUE_URL, prompt=PROMPT, files=[{"path": "README.md", "ref": "main"}])

    assert run["outcome"] == "success"
    assert statuses(run) == [
        ("runtime", "ok"), ("mapping", "ok"), ("mapping", "ok"),
        ("task", "ok"), ("issue", "ok"), ("file", "ok"), ("task", "info"),
    ]
    task = [e for e in run["events"] if e["step"] == "task"][0]
    assert task["source"] == "agntid"
    task_id = task["detail"]["taskId"]
    assert task["detail"]["shortlist"] == ["fixture_github_get_file_contents", "fixture_github_issue_read"]
    assert task["detail"]["intentEngine"] == "fixture-engine"

    issue = last_event(run, "issue")
    assert issue["source"] == "agntid"
    assert issue["detail"]["decision"] == "allowed" and issue["detail"]["execution"] == "success"
    assert issue["detail"]["value"] == {
        "note": FIXTURE_NOTE, "repo": "fixture-org/fixture-repo", "number": 7, "method": "get"
    }
    assert issue["detail"]["provenance"]["taskId"] == task_id
    file = last_event(run, "file")
    assert file["detail"]["path"] == "README.md"
    assert file["detail"]["value"] == {"result": f"{FIXTURE_NOTE}: fixture-org/fixture-repo/README.md@main"}

    # agntid_task_id on the wire is only ever added by agntid.wrap_client (Module 2).
    assert server.business_calls() == [
        ("fixture_github_issue_read",
         {"method": "get", "owner": "fixture-org", "repo": "fixture-repo", "issue_number": 7, "agntid_task_id": task_id}),
        ("fixture_github_get_file_contents",
         {"owner": "fixture-org", "repo": "fixture-repo", "path": "README.md", "ref": "main", "agntid_task_id": task_id}),
    ]
    assert len(server.called("agntid_task_close")) == 1
    assert run["result"]["issueRead"]["status"] == "success"


async def test_denial_stops_the_run_and_keeps_the_real_reason(github_task_server, tmp_path):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read", "fixture_github_get_file_contents")
    server.denials["fixture_github_issue_read"] = fixture_denial("fixture: repository not allowed", "fixture-repo-scope")
    with serve_web(await _settings(server, tmp_path)) as web:
        run = web.run("investigation", issue="fixture-org/fixture-repo#7", prompt=PROMPT, files=[{"path": "a.py"}])

    assert run["outcome"] == "denied"
    issue = last_event(run, "issue")
    assert (issue["source"], issue["status"]) == ("agntid", "denied")
    assert issue["detail"]["decision"] == "denied" and issue["detail"]["execution"] == "blocked"
    assert issue["detail"]["denial"]["policy_name"] == "fixture-repo-scope"
    assert "fixture: repository not allowed" in issue["detail"]["denialExplanation"]
    skipped = last_event(run, "file")
    assert skipped["status"] == "info" and skipped["detail"]["attempted"] is False
    assert [name for name, _ in server.business_calls()] == ["fixture_github_issue_read"]  # no other tool after a denial


async def test_tool_left_out_of_the_shortlist_is_never_called(github_task_server, tmp_path):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_get_repository")
    with serve_web(await _settings(server, tmp_path)) as web:
        run = web.run("investigation", issue=ISSUE_URL, prompt=PROMPT)
    issue = last_event(run, "issue")
    assert run["outcome"] == "not_in_task"
    assert (issue["source"], issue["status"]) == ("patchpilot", "warning")
    assert issue["detail"]["callSent"] is False
    assert issue["detail"]["decision"] == "not_observed" and issue["detail"]["execution"] == "not_sent"
    assert server.business_calls() == []


def test_without_a_mapping_the_issue_stage_is_unavailable_and_nothing_connects(closed_port_url):
    # A closed port: any connection attempt would surface as a runtime error event.
    with serve_web(make_settings(agntid_env(closed_port_url))) as web:
        run = web.run("investigation", issue=ISSUE_URL, prompt=PROMPT)
    assert run["outcome"] == "unavailable"
    assert statuses(run) == [("issue", "warning")]
    assert "capability map" in last_event(run, "issue")["title"]


@pytest.mark.parametrize(
    ("response", "stage"),
    [
        ({"ok": False, "reason": "fixture: task rejected"}, "task_open_rejected"),
        ({"ok": True, "intent": {"prepared": False, "reason": "fixture: intent disabled"}}, "intent_unavailable"),
    ],
)
async def test_task_not_opened_reports_its_stage_and_sends_nothing(github_task_server, tmp_path, response, stage):
    server = github_task_server
    server.task_open_response = response
    with serve_web(await _settings(server, tmp_path)) as web:
        run = web.run("investigation", issue=ISSUE_URL, prompt=PROMPT)
    assert run["outcome"] == "task_not_opened"
    failure = last_event(run, "task")
    assert (failure["source"], failure["status"]) == ("agntid", "error")
    assert failure["detail"]["stage"] == stage
    assert "fixture:" in failure["detail"]["reason"] + failure["detail"]["intentReason"]
    assert "issue" not in [step for step, _ in statuses(run)]
    assert server.business_calls() == []


async def test_stale_mapping_stops_before_a_task_is_opened(github_task_server, tmp_path):
    server = github_task_server
    with serve_web(await _settings(server, tmp_path, stale=("get_issue",))) as web:
        run = web.run("investigation", issue=ISSUE_URL, prompt=PROMPT)
    assert run["outcome"] == "invalid_mapping"
    mapping = [e for e in run["events"] if e["step"] == "mapping"][0]
    assert mapping["status"] == "error"
    assert any("inputSchema changed since review" in p for p in mapping["detail"]["schemaCheck"]["problems"])
    assert server.calls == []  # no task open, no read


async def test_timeout_is_reported_as_unknown_not_as_a_result(github_task_server, tmp_path):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read")
    server.delays["fixture_github_issue_read"] = 3.0
    settings = await _settings(server, tmp_path)
    settings = make_settings({**settings.environ, "AGNTID_TIMEOUT_SECONDS": "1"})
    with serve_web(settings) as web:
        run = web.run("investigation", issue=ISSUE_URL, prompt=PROMPT)
    issue = last_event(run, "issue")
    assert run["outcome"] == "outcome_unknown"
    assert issue["status"] == "warning"
    assert issue["detail"]["decision"] == "unknown" and issue["detail"]["execution"] == "unknown"
    assert len(server.called("fixture_github_issue_read")) == 1
    assert len(server.called("agntid_task_close")) == 1


async def test_only_one_agntid_run_at_a_time(github_task_server, tmp_path):
    server = github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read")
    server.delays["fixture_github_issue_read"] = 1.5
    with serve_web(await _settings(server, tmp_path)) as web:
        first = web.start("investigation", issue=ISSUE_URL, prompt=PROMPT)
        assert first.status_code == 202
        deadline = time.monotonic() + 5
        while not server.called("fixture_github_issue_read") and time.monotonic() < deadline:
            time.sleep(0.05)
        second = web.start("probe", prompt="Read a ticket")
        events, final = web.follow(first.json()["run"]["id"])
        third = web.start("investigation", issue=ISSUE_URL, prompt=PROMPT)
    assert second.status_code == 409 and second.json()["error"]["code"] == "busy"
    assert final["outcome"] == "success"
    assert third.status_code == 202  # the lane is released when a run ends


@pytest.mark.parametrize(
    "body",
    [
        {"issue": "", "prompt": PROMPT},
        {"issue": "fixture-org/fixture-repo", "prompt": PROMPT},
        {"issue": "https://github.com/fixture-org/fixture-repo/pull/7", "prompt": PROMPT},
        {"issue": "https://gitlab.com/fixture-org/fixture-repo/issues/7", "prompt": PROMPT},
        {"issue": "fixture-org/fixture-repo#0", "prompt": PROMPT},
        {"issue": "../fixture-repo#7", "prompt": PROMPT},
        {"issue": "fixture-org/fixture-repo#7 and more", "prompt": PROMPT},
        {"issue": ISSUE_URL, "prompt": "   "},
        {"issue": ISSUE_URL, "prompt": PROMPT, "files": [{"path": "/etc/passwd"}]},
        {"issue": ISSUE_URL, "prompt": PROMPT, "files": [{"path": "src/../../secret"}]},
        {"issue": ISSUE_URL, "prompt": PROMPT, "files": [{"path": "a.py", "ref": "-x"}]},
        {"issue": ISSUE_URL, "prompt": PROMPT, "files": [{"path": f"f{i}.py"} for i in range(6)]},
        {"issue": ISSUE_URL, "prompt": PROMPT, "testCommand": "rm -rf /"},
    ],
)
def test_invalid_requests_are_rejected_before_any_run(closed_port_url, body):
    with serve_web(make_settings(agntid_env(closed_port_url))) as web:
        response = web.start("investigation", **body)
        runs = web.get("/api/runs").json()["runs"]
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert runs == []


def test_issue_references_accept_url_and_short_forms(closed_port_url):
    with serve_web(make_settings(agntid_env(closed_port_url))) as web:
        url = web.run("investigation", issue=ISSUE_URL + "/#issuecomment-1", prompt=PROMPT)
        short = web.run("investigation", issue="fixture-org/fixture-repo#7", prompt=PROMPT)
    expected = {"owner": "fixture-org", "repo": "fixture-repo", "number": 7, "reference": "fixture-org/fixture-repo#7"}
    assert url["request"]["issue"] == expected and short["request"]["issue"] == expected
