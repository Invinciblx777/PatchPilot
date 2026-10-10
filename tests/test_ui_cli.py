"""patchpilot-ui: operator flags, startup refusals, and static serving.

The end-to-end test starts the real command in a subprocess on a free
loopback port and talks to it over HTTP. No AgntID runtime is contacted.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from patchpilot.ui_cli import build_parser, build_settings, run

from .web_helpers import make_settings, serve_web


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = run(argv, environ={}, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    "origin", ["http://ui.example.dev", "https://", "https://ui.example.dev/app", "https://ui.example.dev/?x=1", "ui.example.dev"]
)
def test_public_origin_must_be_a_bare_https_origin(origin):
    out, err = io.StringIO(), io.StringIO()
    code = run(["--public-origin", origin, "--port", "0"], environ={"PATCHPILOT_ACCESS_KEY": "k" * 32}, stdout=out, stderr=err)
    assert code == 2 and "--public-origin must be an https origin" in err.getvalue()


@pytest.mark.parametrize("key", [None, "", "short-key"])
def test_public_origin_needs_a_long_access_key(key):
    environ = {} if key is None else {"PATCHPILOT_ACCESS_KEY": key}
    out, err = io.StringIO(), io.StringIO()
    code = run(["--public-origin", "https://ui.example.dev", "--port", "0"], environ=environ, stdout=out, stderr=err)
    assert code == 2 and "PATCHPILOT_ACCESS_KEY" in err.getvalue()


def test_public_mode_serves_behind_the_access_key(tmp_path):
    key = "k" * 32
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGNTID_", "PATCHPILOT_"))}
    env["PATCHPILOT_ACCESS_KEY"] = key
    proc = subprocess.Popen(
        [sys.executable, "-m", "patchpilot.ui_cli", "--port", "0", "--env-file", str(tmp_path / "none.env"),
         "--public-origin", "https://UI.example.dev/"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=tmp_path,
    )
    try:
        url = proc.stdout.readline().split(": ", 1)[1].strip()
        assert "https://ui.example.dev may drive" in proc.stdout.readline()
        assert httpx.get(f"{url}/api/session", timeout=10).status_code == 401
        session = httpx.get(f"{url}/api/session", auth=("patchpilot", key), timeout=10)
        assert session.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=20)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
def test_non_loopback_hosts_are_refused(host):
    code, _, err = _run(["--host", host, "--port", "0"])
    assert code == 2
    assert "loopback" in err


def test_repositories_must_be_existing_directories(tmp_path):
    code, _, err = _run(["--repo", str(tmp_path / "missing"), "--port", "0"])
    assert code == 2 and "missing" in err
    code, _, err = _run(["--repo", str(tmp_path), "--repo", str(tmp_path), "--port", "0"])
    assert code == 2 and "more than once" in err


def test_bad_test_configuration_is_a_usage_error(tmp_path):
    config = tmp_path / "tests.json"
    config.write_text("{not json")
    code, _, err = _run(["--test-config", str(config), "--port", "0"])
    assert code == 2 and "not valid JSON" in err
    code, _, err = _run(["--test-command", "pytest 'unclosed", "--port", "0"])
    assert code == 2


def test_settings_come_only_from_operator_flags(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    config = tmp_path / "tests.json"
    config.write_text(json.dumps({"commands": [{"name": "unit", "argv": ["python", "-m", "pytest"], "timeout_seconds": 60}]}))
    args = build_parser().parse_args([
        "--repo", str(first), "--repo", str(second),
        "--test-config", str(config), "--test-command", "ruff check .", "--test-timeout", "30",
        "--allow-sensitive-path", "config/*.key", "--mapping", str(tmp_path / "map.json"),
        "--env-file", str(tmp_path / "custom.env"),
    ])
    settings = build_settings(args, {"AGNTID_MCP_URL": "http://localhost:8082/mcp"})
    assert [(r.id, r.name, r.path) for r in settings.repos] == [("r1", "one", first.resolve()), ("r2", "two", second.resolve())]
    assert [(t.name, t.argv, t.timeout_seconds) for t in settings.tests] == [
        ("unit", ("python", "-m", "pytest"), 60),
        ("command-2", ("ruff", "check", "."), 30.0),
    ]
    assert settings.policy.allowed_sensitive_patterns == ("config/*.key",)
    assert settings.mapping_path == tmp_path / "map.json"
    assert settings.env_file == tmp_path / "custom.env"
    assert settings.environ == {"AGNTID_MCP_URL": "http://localhost:8082/mcp"}


def test_command_serves_the_api_on_a_free_loopback_port(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGNTID_", "PATCHPILOT_"))}
    proc = subprocess.Popen(
        [sys.executable, "-m", "patchpilot.ui_cli", "--port", "0", "--env-file", str(tmp_path / "none.env")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=tmp_path,
    )
    try:
        line = proc.stdout.readline()
        assert line.startswith("PatchPilot UI: http://127.0.0.1:"), line + proc.stderr.read()
        url = line.split(": ", 1)[1].strip()
        session = httpx.get(f"{url}/api/session", timeout=10).json()
        config = httpx.get(f"{url}/api/config", headers={"X-PatchPilot-Session": session["token"]}, timeout=10).json()
        assert config["agntid"]["ok"] is False  # nothing configured: reported, not fatal
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_built_frontend_is_served_with_a_single_page_fallback(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>PatchPilot</title><div id=app></div>")
    (static / "assets" / "app-1a2b.js").write_text("console.log('patchpilot')")
    (static / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "secret.txt").write_text("outside the static directory")
    with serve_web(make_settings(static_dir=static)) as web:
        index = web.get("/")
        deep = web.get("/security")
        asset = web.get("/assets/app-1a2b.js")
        icon = web.get("/favicon.svg")
        missing = web.get("/assets/missing.js")
        escape = web.get("/assets/..%2f..%2fsecret.txt")
    assert index.status_code == 200 and "<div id=app>" in index.text
    assert index.headers["cache-control"] == "no-cache"
    assert deep.status_code == 200 and deep.text == index.text
    assert asset.status_code == 200 and "javascript" in asset.headers["content-type"]
    assert icon.status_code == 200 and "svg" in icon.headers["content-type"]
    assert missing.status_code == 404
    assert "outside the static directory" not in escape.text


def test_missing_build_says_how_to_build(tmp_path):
    with serve_web(make_settings(static_dir=tmp_path / "not-built")) as web:
        response = web.get("/")
    assert response.status_code == 503
    assert "npm --prefix frontend run build" in response.text


def test_default_static_directory_is_inside_the_package():
    settings = build_settings(build_parser().parse_args([]), {})
    assert settings.static_dir == Path(__import__("patchpilot.web").web.__file__).parent / "static"
