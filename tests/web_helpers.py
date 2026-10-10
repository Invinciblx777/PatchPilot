"""Serve the PatchPilot web app on a real local socket for the web tests.

The app runs under a real uvicorn server on 127.0.0.1 in a background thread,
and the tests talk to it with httpx over real HTTP, the way the browser does.
AgntID-facing tests point it at the fixture MCP servers in conftest.py, which
are NOT AgntID; patch tests use real temporary Git repositories.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from patchpilot.patch_workspace import PatchPolicy
from patchpilot.web.app import create_app
from patchpilot.web.config import RepoEntry, UISettings

SESSION_HEADER = "X-PatchPilot-Session"
AGENT_ID = "patchpilot-tests:v1"
USER_ID = "user-7f3a91"


def agntid_env(url: str, **extra: str) -> dict[str, str]:
    """Process environment for a fixture runtime (NOT AgntID)."""
    return {
        "AGNTID_MCP_URL": url,
        "AGNTID_TIMEOUT_SECONDS": "10",
        "PATCHPILOT_AGENT_ID": AGENT_ID,
        "PATCHPILOT_USER_ID": USER_ID,
        **extra,
    }


def make_settings(
    environ: dict[str, str] | None = None,
    *,
    repos: dict[str, Path] | None = None,
    tests: tuple = (),
    mapping_path: Path | None = None,
    env_file: Path | None = None,
    static_dir: Path | None = None,
    policy: PatchPolicy = PatchPolicy(),
) -> UISettings:
    return UISettings(
        environ=environ or {},
        env_file=env_file,
        mapping_path=mapping_path,
        repos=tuple(RepoEntry(id=rid, path=path.resolve(), name=path.name) for rid, path in (repos or {}).items()),
        tests=tuple(tests),
        policy=policy,
        static_dir=static_dir,
    )


@dataclass
class WebApp:
    base_url: str
    port: int
    token: str
    http: httpx.Client = field(repr=False)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(path, **kwargs)

    def post(self, path: str, body: Any, **kwargs: Any) -> httpx.Response:
        return self.http.post(path, json=body, **kwargs)

    def start(self, kind: str, **body: Any) -> httpx.Response:
        return self.post("/api/runs", {"kind": kind, **body})

    def follow(self, run_id: str, after: int = 0) -> tuple[list[dict], dict]:
        """Read the NDJSON event stream to its end: (events, final run)."""
        events: list[dict] = []
        final: dict | None = None
        with self.http.stream("GET", f"/api/runs/{run_id}/events", params={"after": after}) as response:
            assert response.status_code == 200, response.read()
            assert response.headers["content-type"].startswith("application/x-ndjson")
            for line in response.iter_lines():
                if not line.strip():
                    continue
                message = json.loads(line)
                if message["type"] == "event":
                    events.append(message["event"])
                elif message["type"] == "end":
                    final = message["run"]
        assert final is not None, "stream ended without an end message"
        return events, final

    def run(self, kind: str, **body: Any) -> dict:
        """Start a run, follow it to the end, and return the final run with its events."""
        response = self.start(kind, **body)
        assert response.status_code == 202, response.text
        events, final = self.follow(response.json()["run"]["id"])
        assert [e["seq"] for e in events] == [e["seq"] for e in final["events"]]
        return final


def _free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


@contextmanager
def serve_web(
    settings: UISettings, *, public_origins: tuple[str, ...] = (), access_key: str | None = None
) -> Iterator[WebApp]:
    sock = _free_socket()
    port = sock.getsockname()[1]
    app = create_app(settings, port=port, public_origins=public_origins, access_key=access_key)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("web app did not start")
        time.sleep(0.02)
    base_url = f"http://127.0.0.1:{port}"
    auth = None if access_key is None else httpx.BasicAuth("patchpilot", access_key)
    token = httpx.get(f"{base_url}/api/session", auth=auth).json()["token"]
    http = httpx.Client(base_url=base_url, headers={SESSION_HEADER: token}, auth=auth, timeout=60)
    try:
        yield WebApp(base_url=base_url, port=port, token=token, http=http)
    finally:
        http.close()
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()


def statuses(run: dict, step: str | None = None) -> list[tuple[str, str]]:
    """(step, status) pairs of a run's events, optionally for one step."""
    return [(e["step"], e["status"]) for e in run["events"] if step is None or e["step"] == step]


def last_event(run: dict, step: str) -> dict:
    matches = [e for e in run["events"] if e["step"] == step]
    assert matches, f"no {step!r} event in {statuses(run)}"
    return matches[-1]
