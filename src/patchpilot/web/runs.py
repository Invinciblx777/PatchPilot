"""Runs, events, and the in-memory activity log of one web UI process.

A run is one operation (runtime check, investigation, task probe, patch check,
patch apply). It records events as things actually happen. Each event says who
produced the fact it reports:

    agntid      data returned by the AgntID runtime: the tool list, a task-open
                result and its shortlist, a denial, a tool result
    patchpilot  PatchPilot's own actions and application checks: configuration,
                mapping and argument checks, patch parsing, git, test commands

The log lives in memory and covers this server process only. It is not an
AgntID audit trail; the runtime keeps its own in the AgntID portal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import secrets
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

AGNTID = "agntid"
PATCHPILOT = "patchpilot"
EVENT_STATUSES = frozenset({"info", "ok", "warning", "denied", "error"})

MAX_RUNS = 200
MAX_EVENTS = 5000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def jsonable(value: Any, _depth: int = 0) -> Any:
    """Convert SDK and module values (dataclasses, enums, tuples) into plain JSON data."""
    if _depth > 64:
        return "<nested too deeply>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Enum):
        return jsonable(value.value, _depth + 1)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, _depth + 1) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name), _depth + 1) for f in dataclasses.fields(value)}
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(), _depth + 1)
    return str(value)


@dataclasses.dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    at: str
    source: str
    step: str
    status: str
    title: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "runId": self.run_id,
            "at": self.at,
            "source": self.source,
            "step": self.step,
            "status": self.status,
            "title": self.title,
            "detail": self.detail,
        }


class Busy(Exception):
    """Another run already holds the lane this run needs."""


class Run:
    def __init__(self, registry: RunRegistry, run_id: str, kind: str, title: str, request: dict[str, Any]):
        self._registry = registry
        self.id = run_id
        self.kind = kind
        self.title = title
        self.request = request
        self.state = "running"
        self.outcome: str | None = None
        self.result: dict[str, Any] | None = None
        self.started_at = utc_now()
        self.finished_at: str | None = None
        self.events: list[Event] = []
        self._changed = asyncio.Event()

    @property
    def done(self) -> bool:
        return self.state != "running"

    def emit(self, source: str, step: str, status: str, title: str, detail: Mapping[str, Any] | None = None) -> Event:
        """Record one event. Must be called on the event loop thread."""
        if source not in (AGNTID, PATCHPILOT) or status not in EVENT_STATUSES:
            raise ValueError(f"invalid event source/status {source!r}/{status!r}")
        event = self._registry._record(self, source, step, status, title, jsonable(dict(detail or {})))
        self.events.append(event)
        self._touch()
        return event

    def finish(self, outcome: str, result: Mapping[str, Any] | None = None) -> None:
        if self.done:
            return
        self.outcome = outcome
        self.result = None if result is None else jsonable(dict(result))
        self.state = "finished"
        self.finished_at = utc_now()
        self._touch()

    def changed(self) -> asyncio.Event:
        """An event that is set at the next change (new event or finish)."""
        return self._changed

    def _touch(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "outcome": self.outcome,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "eventCount": len(self.events),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "request": self.request,
            "events": [e.to_dict() for e in self.events],
            "result": self.result,
        }


class RunRegistry:
    """All runs of this process, the activity log, and the lanes that serialize runs.

    Lanes: "agntid" (one task at a time: the SDK's get_last_denial is
    thread-local, so concurrent tasks could swap denials) and "patch" (one
    workspace at a time).
    """

    def __init__(self) -> None:
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._activity: deque[Event] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._lanes: dict[str, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def create(self, kind: str, title: str, request: Mapping[str, Any], *, lane: str | None = None) -> Run:
        if lane is not None and lane in self._lanes:
            raise Busy(lane)
        run = Run(self, f"run-{secrets.token_hex(6)}", kind, title, jsonable(dict(request)))
        if lane is not None:
            self._lanes[lane] = run.id
        self._runs[run.id] = run
        while len(self._runs) > MAX_RUNS:
            oldest_id, oldest = next(iter(self._runs.items()))
            if not oldest.done:
                break
            del self._runs[oldest_id]
        return run

    def release(self, run: Run) -> None:
        for lane, owner in list(self._lanes.items()):
            if owner == run.id:
                del self._lanes[lane]

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def runs(self) -> list[Run]:
        return list(reversed(self._runs.values()))

    def activity(self, after: int = 0) -> list[Event]:
        return [e for e in self._activity if e.seq > after]

    def latest_seq(self) -> int:
        return self._seq

    def _record(self, run: Run, source: str, step: str, status: str, title: str, detail: dict[str, Any]) -> Event:
        self._seq += 1
        event = Event(self._seq, run.id, utc_now(), source, step, status, title, detail)
        self._activity.append(event)
        return event

    def spawn(self, run: Run, work: Callable[[Run], Awaitable[None]]) -> None:
        """Execute `work(run)` in the background; the run always finishes and releases its lane."""
        task = asyncio.get_running_loop().create_task(self._execute(run, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute(self, run: Run, work: Callable[[Run], Awaitable[None]]) -> None:
        try:
            await work(run)
        except asyncio.CancelledError:
            run.emit(PATCHPILOT, "server", "error", "Stopped because the PatchPilot server shut down")
            run.finish("interrupted")
            raise
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never shown as success
            logger.exception("run %s failed", run.id)
            run.emit(PATCHPILOT, "server", "error", "Unexpected PatchPilot error", {"type": type(exc).__name__})
            run.finish("internal_error")
        finally:
            if not run.done:
                run.finish("internal_error")
            self.release(run)

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
