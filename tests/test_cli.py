"""CLI report output against local fixture MCP servers (NOT AgntID; see conftest.py)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from patchpilot.discover_cli import format_tool, run
from patchpilot.agntid_connector import ToolInfo

from .conftest import FIXTURE_TOKEN


def _run(argv, environ, tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    code = run([*argv, "--env-file", str(tmp_path / "absent.env")], environ=environ, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_text_report_lists_real_names_descriptions_and_schemas(tool_server, tmp_path):
    code, out, err = _run([], {"AGNTID_MCP_URL": tool_server.url}, tmp_path)
    assert code == 0, err
    assert "outcome:       OK" in out
    assert "tools:         3 returned" in out
    for name in ("fixture_add_numbers", "fixture_srv__create_note", "fixture_no_description"):
        assert name in out
    assert "Add two numbers." in out
    assert '"first_number"' in out
    assert "description: (none returned)" in out
    assert "Visible is not authorized" in out


def test_json_report_round_trips(tool_server, tmp_path):
    code, out, _ = _run(["--json"], {"AGNTID_MCP_URL": tool_server.url}, tmp_path)
    assert code == 0
    report = json.loads(out)
    assert report["ok"] is True
    assert report["toolCount"] == 3
    add = next(t for t in report["tools"] if t["name"] == "fixture_add_numbers")
    assert set(add["inputSchema"]["required"]) == {"first_number", "second_number"}


def test_empty_list_is_reported_explicitly(empty_server, tmp_path):
    code, out, _ = _run([], {"AGNTID_MCP_URL": empty_server.url}, tmp_path)
    assert code == 0
    assert "tools:         0 returned" in out
    assert "EMPTY tool list" in out


def test_missing_config_exit_code_and_message(tmp_path):
    code, out, err = _run([], {}, tmp_path)
    assert code == 2
    assert out == ""
    assert "FAILED (configuration_error)" in err
    assert "No tools are reported" in err


def test_unreachable_exit_code(closed_port_url, tmp_path):
    code, _, err = _run([], {"AGNTID_MCP_URL": closed_port_url}, tmp_path)
    assert code == 3
    assert "FAILED (runtime_unreachable) during connect" in err


def test_auth_failure_output_never_contains_token(auth_server, tmp_path):
    secret = "cli-secret-token-55aa"
    env = {"AGNTID_MCP_URL": auth_server.url, "AGNTID_ACCESS_TOKEN": secret}
    for argv in ([], ["--json"]):
        code, out, err = _run(argv, env, tmp_path)
        assert code == 4
        assert secret not in out and secret not in err


def test_success_output_never_contains_token(auth_server, tmp_path):
    env = {"AGNTID_MCP_URL": auth_server.url, "AGNTID_ACCESS_TOKEN": FIXTURE_TOKEN}
    for argv in ([], ["--json"]):
        code, out, err = _run(argv, env, tmp_path)
        assert code == 0
        assert FIXTURE_TOKEN not in out and FIXTURE_TOKEN not in err


def test_env_file_is_used_when_environment_is_unset(tool_server, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGNTID_MCP_URL={tool_server.url}\n")
    out, err = io.StringIO(), io.StringIO()
    code = run(["--env-file", str(env_file)], environ={}, stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    assert f"AGNTID_MCP_URL={env_file}" in out.getvalue()


def test_terminal_escape_sequences_in_descriptions_are_neutralized():
    tool = ToolInfo(name="x\x1b[31m", description="evil\x1b]0;title\x07", input_schema={"type": "object"})
    text = format_tool(1, tool)
    assert "\x1b" not in text and "\x07" not in text
    assert "\\x1b" in text
