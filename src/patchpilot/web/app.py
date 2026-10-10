"""Starlette application for the PatchPilot web UI.

Routes call the existing modules through web/operations.py; nothing here talks
to GitHub, to the raw MCP client, or to the AgntID runtime UI. web/guard.py
applies the local-only request checks before any route runs.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from patchpilot import __version__
from patchpilot.web.config import UISettings, config_view
from patchpilot.web.guard import LocalGuard
from patchpilot.web.operations import (
    investigate,
    patch_digest,
    patch_workflow,
    probe,
    repository_view,
    runtime_report,
)
from patchpilot.web.requests import (
    RequestError,
    invalid,
    investigation_request,
    patch_apply_request,
    patch_check_request,
    probe_request,
)
from patchpilot.web.runs import Busy, Run, RunRegistry, utc_now

# A patch is at most 2 MiB (patch_workspace.MAX_PATCH_BYTES); JSON escaping can
# grow it, so the request limit leaves room for that and nothing more.
MAX_BODY_BYTES = 8 * 1024 * 1024
HEARTBEAT_SECONDS = 15


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class JSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return _dumps(content).encode("utf-8")


async def read_json(request: Request) -> Any:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise RequestError(413, "body_too_large", f"request bodies are limited to {MAX_BODY_BYTES} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise RequestError(413, "body_too_large", f"request bodies are limited to {MAX_BODY_BYTES} bytes")
    try:
        return json.loads(body)
    except ValueError:
        raise invalid("the request body is not valid JSON") from None


def _int_param(request: Request, name: str, default: int) -> int:
    value = request.query_params.get(name)
    if value is None:
        return default
    if not value.isdigit():
        raise invalid(f"{name} must be a non-negative integer")
    return int(value)


NOT_BUILT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PatchPilot</title></head>
<body><main><h1>PatchPilot UI is not built</h1>
<p>Build the frontend, then reload this page:</p>
<pre>npm --prefix frontend ci
npm --prefix frontend run build</pre></main></body></html>
"""

# A starter validates one request body and returns (lane, title, request view, work).
Starter = Callable[[dict[str, Any]], tuple[str | None, str, dict[str, Any], Callable[[Run], Awaitable[None]]]]


def create_app(settings: UISettings, *, port: int) -> Starlette:
    token = secrets.token_urlsafe(32)
    started_at = utc_now()
    registry = RunRegistry()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await registry.shutdown()

    def start_investigation(body: dict[str, Any]):
        request = investigation_request(body)
        title = f"Investigate {request.issue.reference}"
        return "agntid", title, request.view(), lambda run: investigate(settings, run, request)

    def start_probe(body: dict[str, Any]):
        request = probe_request(body)
        title = f"Task probe: {request.call.tool}" if request.call else "Task probe (shortlist only)"
        return "agntid", title, request.view(), lambda run: probe(settings, run, request)

    def start_patch(body: dict[str, Any], *, apply: bool):
        request = patch_apply_request(body) if apply else patch_check_request(body)
        repo = settings.repo(request.repo_id)
        if repo is None:
            raise invalid("repoId does not name a repository configured with --repo")
        if apply and patch_digest(request.patch) != request.patch_sha256:
            raise RequestError(
                409, "patch_changed", "the patch differs from the one that was checked; check it again before applying"
            )
        title = f"{'Apply' if apply else 'Check'} patch on {repo.name} at {request.base}"
        return "patch", title, request.view(), lambda run: patch_workflow(settings, run, request, repo, apply=apply)

    starters: dict[str, Starter] = {
        "investigation": start_investigation,
        "probe": start_probe,
        "patch_check": lambda body: start_patch(body, apply=False),
        "patch_apply": lambda body: start_patch(body, apply=True),
    }

    async def session(request: Request) -> Response:
        return JSON({"token": token, "startedAt": started_at, "version": __version__})

    async def config(request: Request) -> Response:
        return JSON(await run_in_threadpool(config_view, settings))

    async def repos(request: Request) -> Response:
        views = await run_in_threadpool(lambda: [repository_view(r) for r in settings.repos])
        return JSON({"repos": views})

    async def runtime(request: Request) -> Response:
        run = registry.create("runtime_check", "Runtime check", {})
        return JSON(await runtime_report(settings, run))

    async def activity(request: Request) -> Response:
        after = _int_param(request, "after", 0)
        events = [e.to_dict() for e in registry.activity(after)]
        return JSON({"events": events, "latestSeq": registry.latest_seq()})

    async def start_run(request: Request) -> Response:
        body = await read_json(request)
        kind = body.get("kind") if isinstance(body, dict) else None
        starter = starters.get(kind) if isinstance(kind, str) else None
        if starter is None:
            raise invalid(f"kind must be one of {', '.join(sorted(starters))}")
        lane, title, request_view, work = starter(body)
        try:
            run = registry.create(kind, title, request_view, lane=lane)
        except Busy:
            raise RequestError(
                409, "busy", "another operation that uses this lane is still running; wait for it to finish"
            ) from None
        registry.spawn(run, work)
        return JSON({"run": run.summary()}, status_code=202)

    async def list_runs(request: Request) -> Response:
        return JSON({"runs": [r.summary() for r in registry.runs()]})

    def _run(request: Request) -> Run:
        run = registry.get(request.path_params["run_id"])
        if run is None:
            raise RequestError(404, "not_found", "no such run in this server process")
        return run

    async def get_run(request: Request) -> Response:
        return JSON(_run(request).to_dict())

    async def run_events(request: Request) -> Response:
        run = _run(request)
        after = _int_param(request, "after", 0)

        async def stream() -> AsyncIterator[str]:
            index = after
            while True:
                changed = run.changed()
                pending = run.events[index:]
                for event in pending:
                    yield _dumps({"type": "event", "event": event.to_dict()}) + "\n"
                index += len(pending)
                if run.done and index >= len(run.events):
                    yield _dumps({"type": "end", "run": run.to_dict()}) + "\n"
                    return
                try:
                    await asyncio.wait_for(changed.wait(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield _dumps({"type": "heartbeat"}) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    async def api_not_found(request: Request) -> Response:
        raise RequestError(404, "not_found", f"no API route {request.url.path}")

    def built_static_dir() -> Path | None:
        static = settings.static_dir
        return static if static is not None and (static / "index.html").is_file() else None

    async def index(request: Request) -> Response:
        """The built single-page app: root-level files as themselves, any other path as index.html."""
        static = built_static_dir()
        if static is None:
            return HTMLResponse(NOT_BUILT_PAGE, status_code=503)
        rest = request.path_params.get("rest", "")
        if rest and "/" not in rest and not rest.startswith("."):
            candidate = static / rest
            if candidate.is_file() and candidate.resolve().parent == static.resolve():
                return FileResponse(candidate)
        return FileResponse(static / "index.html", headers={"cache-control": "no-cache"})

    async def request_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, RequestError)
        return JSON(error.body(), status_code=error.status)

    routes: list[BaseRoute] = [
        Route("/api/session", session, methods=["GET"]),
        Route("/api/config", config, methods=["GET"]),
        Route("/api/runtime", runtime, methods=["GET"]),
        Route("/api/repos", repos, methods=["GET"]),
        Route("/api/activity", activity, methods=["GET"]),
        Route("/api/runs", start_run, methods=["POST"]),
        Route("/api/runs", list_runs, methods=["GET"]),
        Route("/api/runs/{run_id}", get_run, methods=["GET"]),
        Route("/api/runs/{run_id}/events", run_events, methods=["GET"]),
        Route("/api/{rest:path}", api_not_found),
    ]
    if settings.static_dir is not None:
        routes.append(Mount("/assets", app=StaticFiles(directory=settings.static_dir / "assets", check_dir=False)))
    routes.append(Route("/{rest:path}", index, methods=["GET"]))
    return Starlette(
        routes=routes,
        middleware=[Middleware(LocalGuard, port=port, token=token)],
        exception_handlers={RequestError: request_error},
        lifespan=lifespan,
    )
