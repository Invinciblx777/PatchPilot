"""Read-only configuration view (GET /api/config). No runtime is contacted."""

from __future__ import annotations

import json

from patchpilot.patch_verification import TestCommand
from patchpilot.patch_workspace import PatchPolicy

from .web_helpers import AGENT_ID, USER_ID, make_settings, serve_web

SECRET = "tok-SECRET-9f8e7d6c5b4a"


def test_config_reports_settings_without_secret_values(tmp_path):
    environ = {
        "AGNTID_MCP_URL": "http://localhost:8082/mcp?debug=1#frag",
        "AGNTID_ACCESS_TOKEN": SECRET,
        "AGNTID_TIMEOUT_SECONDS": "12.5",
        "PATCHPILOT_AGENT_ID": AGENT_ID,
        "PATCHPILOT_USER_ID": USER_ID,
    }
    with serve_web(make_settings(environ)) as web:
        response = web.get("/api/config")
    assert response.status_code == 200
    assert SECRET not in response.text
    body = response.json()
    assert body["agntid"] == {
        "ok": True,
        "endpoint": "http://localhost:8082/mcp",
        "accessTokenSupplied": True,
        "timeoutSeconds": 12.5,
    }
    assert body["identity"] == {"ok": True, "agentId": AGENT_ID, "userId": USER_ID}


def test_configuration_errors_are_reported_not_raised():
    environ = {"PATCHPILOT_USER_ID": "someone@example.com"}
    with serve_web(make_settings(environ)) as web:
        body = web.get("/api/config").json()
    assert body["agntid"]["ok"] is False
    assert body["agntid"]["error"]["kind"] == "configuration_error"
    assert "AGNTID_MCP_URL" in body["agntid"]["error"]["message"]
    assert body["identity"]["ok"] is False
    assert "someone@example.com" not in json.dumps(body)  # a rejected user ID is never echoed


def test_env_file_fills_unset_values_and_is_reread(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AGNTID_MCP_URL=http://127.0.0.1:9/mcp\nPATCHPILOT_AGENT_ID=from-file\n")
    settings = make_settings({"PATCHPILOT_AGENT_ID": "from-environment"}, env_file=env_file)
    with serve_web(settings) as web:
        first = web.get("/api/config").json()
        env_file.write_text("AGNTID_MCP_URL=http://127.0.0.1:10/mcp\nPATCHPILOT_USER_ID=user-2\n")
        second = web.get("/api/config").json()
    assert first["agntid"]["endpoint"] == "http://127.0.0.1:9/mcp"
    assert first["identity"]["ok"] is False  # user ID missing
    assert "AGNTID_MCP_URL=" + str(env_file) in first["sources"]
    assert "PATCHPILOT_AGENT_ID=environment" in first["sources"]
    assert second["agntid"]["endpoint"] == "http://127.0.0.1:10/mcp"
    assert second["identity"] == {"ok": True, "agentId": "from-environment", "userId": "user-2"}


def test_capability_map_status_is_reported(tmp_path):
    digest = "a" * 64
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"capabilities": {"get_issue": {
        "tool": "fixture_srv.issue_read", "schema_sha256": digest,
        "arguments": {"owner": "owner", "repo": "repo", "issue_number": "number"},
    }}}))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")

    with serve_web(make_settings()) as web:
        unset = web.get("/api/config").json()["capabilityMap"]
    with serve_web(make_settings(mapping_path=good)) as web:
        loaded = web.get("/api/config").json()["capabilityMap"]
    with serve_web(make_settings({"PATCHPILOT_CAPABILITY_MAP": str(bad)})) as web:
        broken = web.get("/api/config").json()["capabilityMap"]

    assert unset == {"configured": False}
    assert loaded["configured"] is True and loaded["ok"] is True and loaded["path"] == str(good)
    assert loaded["capabilities"]["get_issue"] == {
        "tool": "fixture_srv.issue_read",
        "schemaSha256": digest,
        "arguments": {"owner": "owner", "repo": "repo", "issue_number": "number"},
        "fixedArguments": {},
    }
    assert sorted(loaded["capabilities"]) == ["get_issue"]
    assert broken["configured"] is True and broken["ok"] is False
    assert "not valid JSON" in broken["error"]


def test_operator_repositories_tests_and_policy_are_listed(tmp_path, monkeypatch):
    repo = tmp_path / "calc"
    repo.mkdir()
    monkeypatch.setenv("FIXTURE_PASS_VALUE", "do-not-show-7d1e")
    tests = (TestCommand("unit", ("python", "-m", "pytest", "-q"), timeout_seconds=90, pass_env=("FIXTURE_PASS_VALUE",)),)
    policy = PatchPolicy(allowed_sensitive_patterns=("config/*.key",))
    with serve_web(make_settings(repos={"r1": repo}, tests=tests, policy=policy)) as web:
        response = web.get("/api/config")
    body = response.json()
    assert body["repos"] == [{"id": "r1", "name": "calc", "path": str(repo.resolve())}]
    assert body["tests"] == [{
        "name": "unit",
        "argv": ["python", "-m", "pytest", "-q"],
        "display": "python -m pytest -q",
        "timeoutSeconds": 90,
        "passEnv": ["FIXTURE_PASS_VALUE"],
    }]
    assert body["patchPolicy"]["allowedSensitivePatterns"] == ["config/*.key"]
    assert ".env" in body["patchPolicy"]["sensitivePatterns"]
    assert "do-not-show-7d1e" not in response.text
