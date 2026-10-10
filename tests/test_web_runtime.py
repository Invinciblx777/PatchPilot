"""Runtime report (GET /api/runtime) over real HTTP and the real SDK.

The runtimes here are the fixture MCP servers from conftest.py: NOT AgntID.
These tests show that PatchPilot reports what an endpoint returns; they show
nothing about a live AgntID runtime.
"""

from __future__ import annotations

import hashlib
import json

import fastmcp

from .conftest import FIXTURE_TOKEN
from .web_helpers import agntid_env, make_settings, serve_web


def _canonical_sha256(schema: dict) -> str:
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _raw_schemas(url: str) -> dict[str, dict]:
    async with fastmcp.Client(url) as client:
        return {t.name: t.inputSchema for t in await client.list_tools()}


async def test_report_lists_every_tool_with_flags_and_schema_hashes(github_task_server):
    raw = await _raw_schemas(github_task_server.url)
    with serve_web(make_settings(agntid_env(github_task_server.url))) as web:
        report = web.get("/api/runtime").json()
    assert report["ok"] is True
    tools = {t["name"]: t for t in report["tools"]}
    assert set(tools) == set(raw)
    assert report["toolCount"] == len(raw)
    assert tools["agntid_task_open"]["isRuntimeControlTool"] is True
    assert tools["fixture_read_file"]["isRuntimeControlTool"] is False
    assert report["businessToolCount"] == len(raw) - 2  # task open + task close
    issue = tools["fixture_github_issue_read"]
    assert issue["inputSchema"] == raw["fixture_github_issue_read"]
    assert issue["schemaSha256"] == _canonical_sha256(raw["fixture_github_issue_read"])
    assert issue["readOnly"] == "declared"
    assert tools["fixture_github_create_issue"]["readOnly"] == "declares_write"
    assert tools["fixture_read_file"]["readOnly"] == "not_declared"
    assert report["capabilities"]["get_issue"]["candidates"] == ["fixture_github_issue_read"]
    assert report["capabilities"]["get_issue"]["mapping"] is None
    assert report["capabilityMap"] == {"configured": False}


async def test_registered_mappings_are_checked_against_live_schemas(github_task_server, tmp_path):
    raw = await _raw_schemas(github_task_server.url)
    mapping = tmp_path / "capabilities.json"
    mapping.write_text(json.dumps({"capabilities": {
        "get_issue": {
            "tool": "fixture_github_issue_read",
            "schema_sha256": _canonical_sha256(raw["fixture_github_issue_read"]),
            "arguments": {"owner": "owner", "repo": "repo", "issue_number": "issue_number"},
            "fixed_arguments": {"method": "get"},
        },
        "read_file": {
            "tool": "fixture_github_get_file_contents",
            "schema_sha256": "0" * 64,  # reviewed against an older schema
            "arguments": {"owner": "owner", "repo": "repo", "path": "path", "ref": "ref"},
        },
    }}))
    with serve_web(make_settings(agntid_env(github_task_server.url), mapping_path=mapping)) as web:
        report = web.get("/api/runtime").json()
    issue = report["capabilities"]["get_issue"]
    assert issue["listedByRuntime"] is True
    assert issue["schemaCheck"] == {"ok": True, "problems": [], "warnings": []}
    stale = report["capabilities"]["read_file"]
    assert stale["schemaCheck"]["ok"] is False
    assert any("inputSchema changed since review" in p for p in stale["schemaCheck"]["problems"])
    assert report["capabilities"]["get_repository"]["mapping"] is None
    assert report["capabilityMap"] == {"configured": True, "ok": True, "path": str(mapping)}
    assert github_task_server.business_calls() == []  # a report never calls a tool


def test_unreachable_runtime_is_reported_with_its_error_class(closed_port_url):
    with serve_web(make_settings(agntid_env(closed_port_url))) as web:
        report = web.get("/api/runtime").json()
    assert report["ok"] is False
    assert report["error"]["kind"] == "runtime_unreachable"
    assert report["endpoint"] == closed_port_url
    assert "tools" not in report


def test_rejected_token_is_reported_without_the_token(auth_server):
    wrong = "wrong-token-55e1c0ffee"
    with serve_web(make_settings(agntid_env(auth_server.url, AGNTID_ACCESS_TOKEN=wrong))) as web:
        response = web.get("/api/runtime")
    assert response.json()["error"]["kind"] == "authentication_failed"
    assert wrong not in response.text and FIXTURE_TOKEN not in response.text


def test_missing_configuration_is_a_report_not_a_crash():
    with serve_web(make_settings({})) as web:
        report = web.get("/api/runtime").json()
    assert report["ok"] is False and report["error"]["kind"] == "configuration_error"


def test_runtime_checks_are_recorded_with_their_source(tool_server, closed_port_url):
    with serve_web(make_settings(agntid_env(tool_server.url))) as web:
        web.get("/api/runtime")
        ok_events = web.get("/api/activity").json()["events"]
    with serve_web(make_settings(agntid_env(closed_port_url))) as web:
        web.get("/api/runtime")
        failed_events = web.get("/api/activity").json()["events"]
    listed = [e for e in ok_events if e["step"] == "runtime"]
    assert [(e["source"], e["status"]) for e in listed] == [("agntid", "ok")]
    assert listed[0]["detail"]["toolCount"] == 3
    failed = [e for e in failed_events if e["step"] == "runtime"]
    assert [(e["source"], e["status"]) for e in failed] == [("patchpilot", "error")]
