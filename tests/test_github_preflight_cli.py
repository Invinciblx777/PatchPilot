"""patchpilot-github-preflight against LOCAL fixture servers.

The servers are NOT AgntID and their GitHub-shaped tools are NOT GitHub (see
conftest.py). These tests check what the CLI reports for each environment
state; they are not a live GitHub or AgntID result.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from patchpilot.agntid_connector import ConnectorConfig, discover_tools
from patchpilot.github_capabilities import schema_fingerprint
from patchpilot.github_preflight_cli import run

from .conftest import FIXTURE_NOTE, FIXTURE_TOKEN, fixture_accepted, fixture_denial

IDENTITY_ENV = {"PATCHPILOT_AGENT_ID": "patchpilot-tests:v1", "PATCHPILOT_USER_ID": "user-7f3a91"}
RUN_ISSUE = ["--run", "get_issue", "--arg", "owner=fixture-org", "--arg", "repo=fixture-repo", "--arg", "issue_number=7"]


def _run(argv, environ, tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    code = run([*argv, "--env-file", str(tmp_path / "absent.env")], environ=environ, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _write_mapping(server, tmp_path: Path, token=None) -> Path:
    """Write a capability map from the discovered metadata, as a reviewer would."""
    tools = asyncio.run(discover_tools(ConnectorConfig(server.url, access_token=token))).tools
    issue = next(t for t in tools if t.name == "fixture_github_issue_read")
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps(
            {
                "capabilities": {
                    "get_issue": {
                        "tool": issue.name,
                        "schema_sha256": schema_fingerprint(issue.input_schema),
                        "arguments": {"owner": "owner", "repo": "repo", "issue_number": "issue_number"},
                        "fixed_arguments": {"method": "get"},
                    }
                }
            }
        )
    )
    return path


def test_unreachable_runtime_is_a_truthful_failure(closed_port_url, tmp_path):
    code, out, err = _run([], {"AGNTID_MCP_URL": closed_port_url}, tmp_path)
    assert code == 3
    assert out == ""
    assert "FAILED (runtime_unreachable) during connect" in err
    assert "No GitHub tools are reported" in err


def test_runtime_without_github_tools_reports_no_integration(tool_server, tmp_path):
    code, out, _ = _run([], {"AGNTID_MCP_URL": tool_server.url}, tmp_path)
    assert code == 9
    assert "0 GitHub-related" in out
    assert "No usable GitHub integration was discovered" in out
    assert "Operations -> MCP -> Servers -> Add MCP" in out


def test_candidates_without_mapping_stay_disabled(github_task_server, tmp_path):
    code, out, _ = _run([], {"AGNTID_MCP_URL": github_task_server.url}, tmp_path)
    assert code == 9
    assert "CANDIDATES only" in out
    assert "[1] fixture_github_issue_read" in out
    assert "schema_sha256: " in out
    assert '"issue_number"' in out  # full inputSchema printed
    assert out.count("not registered (integration disabled)") == 3
    assert "candidates:            single: fixture_github_issue_read" in out
    assert "fixture_github_create_issue" in out  # listed, but not a read candidate
    assert "candidate for: (no read capability)" in out


def test_verified_mapping_is_not_reported_as_authorized(github_task_server, tmp_path):
    mapping = _write_mapping(github_task_server, tmp_path)
    code, out, _ = _run(["--mapping", str(mapping)], {"AGNTID_MCP_URL": github_task_server.url}, tmp_path)
    assert code == 0
    assert "schema check:          verified" in out
    assert "in task tool list:     not checked" in out
    assert "runtime authorization: not tested" in out
    assert "authorized" not in out.replace("not authorized", "")
    assert github_task_server.business_calls() == []  # no business call without --run


def test_task_filter_exclusion_is_reported(github_task_server, tmp_path):
    github_task_server.task_open_response = fixture_accepted("fixture_github_get_repository")
    mapping = _write_mapping(github_task_server, tmp_path)
    env = {"AGNTID_MCP_URL": github_task_server.url, **IDENTITY_ENV}
    code, out, _ = _run(["--mapping", str(mapping), "--task-prompt", "Read issue 7", "--json"], env, tmp_path)
    report = json.loads(out)
    entry = report["capabilities"]["get_issue"]
    assert (entry["listedByRuntime"], entry["schemaCheck"]["ok"], entry["inTaskToolList"]) == (True, True, False)
    assert report["task"]["toolNames"] == ["fixture_github_get_repository"]
    assert any("shortlist for this prompt excluded" in s for s in report["nextSteps"])
    assert code == 0
    assert github_task_server.business_calls() == []


def test_task_without_intent_snapshot_fails_closed(github_task_server, tmp_path):
    github_task_server.task_open_response = {"ok": True, "intent": {"prepared": False, "reason": "fixture: off"}}
    env = {"AGNTID_MCP_URL": github_task_server.url, **IDENTITY_ENV}
    code, out, _ = _run(["--task-prompt", "Read issue 7"], env, tmp_path)
    assert code == 10
    assert "task NOT opened (intent_unavailable)" in out
    assert "LLM-based intent" in out
    assert github_task_server.business_calls() == []


def test_run_reads_once_through_the_task_and_shows_provenance(github_task_server, tmp_path):
    github_task_server.task_open_response = fixture_accepted("fixture_github_issue_read")
    mapping = _write_mapping(github_task_server, tmp_path)
    env = {"AGNTID_MCP_URL": github_task_server.url, **IDENTITY_ENV}
    code, out, err = _run(["--mapping", str(mapping), "--task-prompt", "Read issue 7", *RUN_ISSUE], env, tmp_path)
    assert code == 0, err
    assert "run get_issue: SUCCESS (call sent: yes)" in out
    assert FIXTURE_NOTE in out
    assert '"dispatchedVia": "TaskSession.call_tool -> agntid wrap_client call_tool_async"' in out
    assert f'"endpoint": "{github_task_server.url}"' in out
    assert "runtime authorization: allowed for this one call" in out
    [(name, args)] = github_task_server.business_calls()
    assert name == "fixture_github_issue_read"
    assert args["issue_number"] == 7 and "agntid_task_id" in args


def test_run_denial_is_reported_and_not_retried(github_task_server, tmp_path):
    github_task_server.task_open_response = fixture_accepted("fixture_github_issue_read")
    github_task_server.denials["fixture_github_issue_read"] = fixture_denial("fixture: denied", "fixture-policy")
    mapping = _write_mapping(github_task_server, tmp_path)
    env = {"AGNTID_MCP_URL": github_task_server.url, **IDENTITY_ENV}
    code, out, _ = _run(["--mapping", str(mapping), "--task-prompt", "Read issue 7", *RUN_ISSUE, "--json"], env, tmp_path)
    assert code == 11
    run_report = json.loads(out)["task"]["run"]
    assert run_report["status"] == "denied"
    assert run_report["denial"]["policy_name"] == "fixture-policy"
    assert json.loads(out)["capabilities"]["get_issue"]["runtimeAuthorization"].startswith("denied for this one call")
    assert len(github_task_server.called("fixture_github_issue_read")) == 1


def test_run_requires_a_task_prompt_and_identity(github_task_server, tmp_path):
    env = {"AGNTID_MCP_URL": github_task_server.url}
    code, _, err = _run(RUN_ISSUE, env, tmp_path)
    assert code == 2 and "--run needs --task-prompt" in err
    code, _, err = _run(["--task-prompt", "x", *RUN_ISSUE], env, tmp_path)
    assert code == 2 and "PATCHPILOT_AGENT_ID" in err
    code, _, err = _run(["--task-prompt", "x", "--run", "get_issue", "--arg", "issue_number=seven"], {**env, **IDENTITY_ENV}, tmp_path)
    assert code == 2 and "must be an integer" in err
    assert github_task_server.calls == []


def test_malformed_mapping_file_is_a_config_error(github_task_server, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"capabilities": {"get_issue": {"tool": "x", "arguments": {}}}}))
    code, _, err = _run(["--mapping", str(bad)], {"AGNTID_MCP_URL": github_task_server.url}, tmp_path)
    assert code == 2
    assert "FAILED (configuration_error)" in err


def test_token_never_appears_in_output_or_errors(auth_github_task_server, tmp_path):
    server = auth_github_task_server
    server.task_open_response = fixture_accepted("fixture_github_issue_read")
    mapping = _write_mapping(server, tmp_path, token=FIXTURE_TOKEN)
    env = {"AGNTID_MCP_URL": server.url, "AGNTID_ACCESS_TOKEN": FIXTURE_TOKEN, **IDENTITY_ENV}
    for extra in ([], ["--json"]):
        code, out, err = _run(["--mapping", str(mapping), "--task-prompt", "Read issue 7", *RUN_ISSUE, *extra], env, tmp_path)
        assert code == 0, err
        assert FIXTURE_TOKEN not in out and FIXTURE_TOKEN not in err
    wrong = "wrong-preflight-token-8d2b"
    for extra in ([], ["--json"]):
        code, out, err = _run(extra, {**env, "AGNTID_ACCESS_TOKEN": wrong}, tmp_path)
        assert code == 4
        assert wrong not in out and wrong not in err
