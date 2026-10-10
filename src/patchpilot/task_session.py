"""Task lifecycle, trusted task identity, and fail-closed tool dispatch.

Builds on the Module 1 connector. Uses only SDK task APIs verified in the pinned
source (agntid 0.2.0, commit f4fcd2fe):

    visible = await agntid.get_tools_list(client)
    task_id = agntid.create_task(agent_id, user_id, prompt)
    wrapped = agntid.wrap_client(client, task_id, task_argument="agntid_task_id")
    async with agntid.task_context(client, task_id):
        task_open = await agntid.send_task_open_details(
            client, task_id, agent_id, user_id, prompt
        )
        task_tools = agntid.filter_tools_for_task(visible, task_open)
        result = await wrapped.call_tool_async(name, arguments)

The task shortlist narrows what an agent may choose. It is not authorization:
the AgntID runtime still evaluates every call sent through the wrapped client.
The dispatcher's own checks (unlisted tool, reserved argument) are an
application-side guard and are never reported as AgntID decisions.

The caller owns the MCP client and its lifecycle, exactly as in Module 1:

    async with agntid.AgntidMCPClient(config.mcp_url, access_token=config.access_token) as client:
        async with open_task_session(client, config, identity, prompt) as session:
            result = await session.call_tool(name, arguments)
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

import agntid

from patchpilot.agntid_connector import (
    ConfigError,
    ConnectorConfig,
    ConnectorError,
    DiscoveryError,
    ToolInfo,
    _scrub,
    classify_error,
    normalize_tool,
)

logger = logging.getLogger(__name__)

# Application configuration names (PatchPilot's own, not AgntID-standard names).
ENV_AGENT_ID = "PATCHPILOT_AGENT_ID"
ENV_USER_ID = "PATCHPILOT_USER_ID"

# The argument the running achr-runtime reads a call's task from
# (tool_execution.py _TASK_CORRELATION_ARG). Its pinned MCP SDK has no
# middleware, so the SDK's default _task_id is never read there and the call
# runs unattributed, with intent validation skipped. wrap_client(...,
# task_argument=TASK_ARGUMENT) sets args[TASK_ARGUMENT] = task_id after copying
# the caller's arguments (agntid mcp_wrapper.py _inject_context).
TASK_ARGUMENT = "agntid_task_id"

# The SDK wrapper's default correlation names (task_argument /
# delegation_argument). The wrapper forwards a caller-supplied _delegation_id
# unchanged, so a caller must never be able to set either one.
RESERVED_ARGUMENT_NAMES = frozenset({"_task_id", "_delegation_id"})
# Prefix of runtime-internal fields (agntid.mcp_wrapper.PLATFORM_PREFIX).
RESERVED_ARGUMENT_PREFIX = "__agntid_"
# AgntID's argument namespace. The runtime adds agntid_task_id,
# agntid_delegation_id, and agntid_approval to every mounted tool's schema and
# pops them before policy evaluation (achr-runtime tool_execution.py,
# _resolve_task_for_execution): the first two choose the task or delegation a
# call is attributed to, and agntid_approval is merged into the policy context
# as approval evidence. None of them is a business input.
RESERVED_NAMESPACE = "agntid_"


def is_reserved_argument(name: str) -> bool:
    """The one rule for argument names a caller may never supply.

    Reserved: the SDK wrapper's correlation names (_task_id, _delegation_id),
    and any name that starts with "agntid_" once leading underscores are
    dropped, in any case (agntid_task_id, agntid_approval, __agntid_*). Only
    agntid.wrap_client adds reserved arguments, after the caller's are checked.
    """
    return name in RESERVED_ARGUMENT_NAMES or name.lstrip("_").casefold().startswith(RESERVED_NAMESPACE)


def _names(names: list[str]) -> str:
    # Names only, never values, each cut short: the caller chose them.
    return ", ".join(repr(n if len(n) <= 64 else n[:64] + "...") for n in sorted(names))

# Stable opaque identifier: no whitespace, no "@" (so no email addresses).
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class TaskSessionError(ConnectorError):
    """Base task-session error. Messages never contain the access token."""

    kind = "task_session_error"


@dataclass(frozen=True)
class TaskOpenFailure:
    """Why a task session was not opened. No business tool was called.

    stage is one of:
      task_open_rejected  the runtime answered task open with ok != true
      task_open_error     the task-open call itself failed (transport, tool error, timeout)
      intent_unavailable  the task opened, but no usable intent snapshot was returned,
                          so the SDK refused to build a task tool list (fail closed)
    """

    stage: str
    task_id: str
    reason: str
    intent_reason: str = ""


class TaskOpenError(TaskSessionError):
    kind = "task_open_failed"

    def __init__(self, failure: TaskOpenFailure, *, cause_type: str | None = None):
        super().__init__(
            f"task {failure.task_id} was not opened ({failure.stage}): {failure.reason}",
            phase="task_open",
            cause_type=cause_type,
        )
        self.failure = failure


class TaskCloseError(TaskSessionError):
    kind = "task_close_failed"


class TaskSessionClosedError(TaskSessionError):
    kind = "task_session_closed"


# --------------------------------------------------------------------------- #
# Trusted identity
# --------------------------------------------------------------------------- #


def _check_identifier(label: str, env_name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"task {label} is missing; set {env_name} from trusted configuration",
            phase="identity",
        )
    # The value is never echoed: a rejected user ID may be personal data.
    if "@" in value:
        raise ConfigError(
            f"task {label} looks like an email address; use a stable opaque identifier",
            phase="identity",
        )
    if not _IDENTIFIER.fullmatch(value):
        raise ConfigError(
            f"task {label} must be 1-128 characters of letters, digits, '.', '_', ':' "
            "or '-', starting with a letter or digit",
            phase="identity",
        )


@dataclass(frozen=True)
class TaskIdentity:
    """Agent and user identity for a task, supplied by the application, never the prompt."""

    agent_id: str
    user_id: str

    def __post_init__(self) -> None:
        _check_identifier("agent_id", ENV_AGENT_ID, self.agent_id)
        _check_identifier("user_id", ENV_USER_ID, self.user_id)


def load_identity(env: Mapping[str, str]) -> TaskIdentity:
    """Build a validated TaskIdentity from trusted configuration (not from prompt text)."""
    return TaskIdentity(
        agent_id=(env.get(ENV_AGENT_ID) or "").strip(),
        user_id=(env.get(ENV_USER_ID) or "").strip(),
    )


# --------------------------------------------------------------------------- #
# Tool call results
# --------------------------------------------------------------------------- #


class CallStatus(str, Enum):
    SUCCESS = "success"  # the wrapped client returned a ToolResult / ToolResultScalar
    DENIED = "denied"  # the wrapped client returned an SDK DenialResult
    REJECTED = "rejected"  # PatchPilot refused before sending anything (application guard)
    ERROR = "error"  # the call was sent and failed for a non-denial reason


@dataclass(frozen=True)
class ToolCallResult:
    status: CallStatus
    tool_name: str
    task_id: str
    value: Any = None
    message: str = ""
    denial: dict[str, Any] | None = None
    denial_explanation: str | None = None
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is CallStatus.SUCCESS

    @property
    def call_sent(self) -> bool:
        """False only for REJECTED: no request reached the runtime."""
        return self.status is not CallStatus.REJECTED


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


class TaskSession:
    """One opened task: its filtered tools and the only path to call them.

    Created by open_task_session; usable only inside that context. The wrapped
    client is deliberately not exposed, so every business call passes the
    dispatcher's checks and goes through wrapped.call_tool_async.
    """

    def __init__(
        self,
        *,
        task_id: str,
        identity: TaskIdentity,
        tools: tuple[ToolInfo, ...],
        excluded_control_tools: tuple[str, ...],
        task_open: agntid.TaskOpenResult,
        wrapped: Any,
        secret: str | None,
    ):
        self.task_id = task_id
        self.identity = identity
        self.tools = tools
        self.excluded_control_tools = excluded_control_tools
        self.task_open = task_open
        self.close_error: str | None = None
        self._tool_names = frozenset(t.name for t in tools)
        self._wrapped = wrapped
        self._secret = secret
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    @property
    def tool_names(self) -> frozenset[str]:
        return self._tool_names

    def _deactivate(self) -> None:
        self._active = False

    def __repr__(self) -> str:
        return (
            f"TaskSession(task_id={self.task_id!r}, active={self._active!r}, "
            f"tools={sorted(self._tool_names)!r})"
        )

    def _rejection(self, name: Any, arguments: Any) -> str | None:
        if not isinstance(name, str) or name not in self._tool_names:
            if isinstance(name, str) and name in self.excluded_control_tools:
                return (
                    f"{name!r} is an AgntID runtime control tool; it is never callable "
                    "through the business-tool dispatcher"
                )
            return f"{name!r} is not in this task's tool list"
        if arguments is None:
            return None
        if not isinstance(arguments, Mapping):
            return "arguments must be a mapping of argument names to values"
        if not all(isinstance(key, str) for key in arguments):
            return "argument names must be strings"
        reserved = [key for key in arguments if is_reserved_argument(key)]
        if reserved:
            return (
                f"argument {_names(reserved)} is reserved for AgntID task, delegation, and approval "
                "context; only the AgntID SDK wrapper adds it"
            )
        return None

    def _result(self, status: CallStatus, name: Any, **fields: Any) -> ToolCallResult:
        tool_name = name if isinstance(name, str) else repr(name)
        result = ToolCallResult(status=status, tool_name=tool_name, task_id=self.task_id, **fields)
        logger.info("task %s: tool %r -> %s", self.task_id, tool_name, status.value)
        return result

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolCallResult:
        """Call one task-listed tool through the wrapped client, exactly once.

        Never retries, never changes the tool or arguments, and never falls back
        to the unwrapped client.
        """
        if not self._active:
            raise TaskSessionClosedError(
                f"task session {self.task_id} is closed; open a new task session",
                phase="tool_call",
            )
        reason = self._rejection(name, arguments)
        if reason is not None:
            return self._result(
                CallStatus.REJECTED,
                name,
                message=f"PatchPilot rejected the call before sending it: {reason}. "
                "This is an application check, not an AgntID decision.",
            )

        call_arguments = copy.deepcopy(dict(arguments or {}))
        # get_last_denial is thread-local, not per call; clear it so a denial read
        # below can only have been stored by this call.
        agntid.get_last_denial(clear=True)
        try:
            raw = await self._wrapped.call_tool_async(name, call_arguments)
        except Exception as exc:  # noqa: BLE001 - reported as a structured, token-free result
            return self._result(
                CallStatus.ERROR,
                name,
                message=_scrub(str(exc), self._secret) or type(exc).__name__,
                error_type=type(exc).__name__,
            )
        return self._interpret(name, raw)

    def _interpret(self, name: str, raw: Any) -> ToolCallResult:
        if getattr(raw, "denied", False) is True:
            denial = agntid.get_last_denial(clear=True)
            explanation = agntid.format_denial(denial)
            if denial is not None:
                denial = {
                    k: _scrub(v, self._secret) if isinstance(v, str) else v
                    for k, v in denial.items()
                }
            return self._result(
                CallStatus.DENIED,
                name,
                message=_scrub(str(getattr(raw, "message", "") or ""), self._secret),
                denial=denial,
                denial_explanation=_scrub(explanation, self._secret) if explanation else None,
            )
        if isinstance(raw, agntid.ToolResult):
            return self._result(CallStatus.SUCCESS, name, value=dict(raw))
        if isinstance(raw, agntid.ToolResultScalar):
            return self._result(CallStatus.SUCCESS, name, value=raw.value)
        # Anything else (None, a raw error dict) is not a documented success shape.
        return self._result(
            CallStatus.ERROR,
            name,
            message=f"wrapped client returned an unrecognized result type {type(raw).__name__}",
            error_type=type(raw).__name__,
        )


# --------------------------------------------------------------------------- #
# Opening and closing
# --------------------------------------------------------------------------- #


async def _visible_tools(client: Any, config: ConnectorConfig) -> tuple[list[Any], tuple[ToolInfo, ...]]:
    # Same guard as discover_tools: get_tools_list returns [] for a client
    # without list_tools/get_tools, which must not look like "no tools".
    if not callable(getattr(client, "list_tools", None)) and not callable(
        getattr(client, "get_tools", None)
    ):
        raise DiscoveryError("SDK client exposes neither list_tools nor get_tools", phase="discover")
    error: ConnectorError | None = None
    try:
        async with asyncio.timeout(config.timeout_seconds):
            raw = await agntid.get_tools_list(client)
    except Exception as exc:  # noqa: BLE001 - re-raised as a classified, token-free error
        error = classify_error(exc, phase="discover", config=config)
    if error is not None:
        raise error  # outside the except block: the httpx cause is not chained
    return raw, tuple(normalize_tool(t) for t in raw)


async def _open_task(
    client: Any, config: ConnectorConfig, identity: TaskIdentity, prompt: str, task_id: str
) -> agntid.TaskOpenResult:
    error: ConnectorError | None = None
    try:
        async with asyncio.timeout(config.timeout_seconds):
            task_open = await agntid.send_task_open_details(
                client, task_id, identity.agent_id, identity.user_id, prompt
            )
    except Exception as exc:  # noqa: BLE001 - re-raised as a classified, token-free error
        error = classify_error(exc, phase="task_open", config=config)
    if error is not None:
        raise TaskOpenError(
            TaskOpenFailure(stage="task_open_error", task_id=task_id, reason=error.message),
            cause_type=error.cause_type,
        )
    if not task_open.ok:
        raise TaskOpenError(
            TaskOpenFailure(
                stage="task_open_rejected",
                task_id=task_id,
                reason=_scrub(task_open.reason, config.access_token)
                or "the runtime did not accept the task and returned no reason",
            )
        )
    return task_open


def _task_tools(
    visible_raw: list[Any],
    visible: tuple[ToolInfo, ...],
    task_open: agntid.TaskOpenResult,
    task_id: str,
) -> tuple[tuple[ToolInfo, ...], tuple[str, ...]]:
    failure: TaskOpenFailure | None = None
    try:
        shortlisted = agntid.filter_tools_for_task(visible_raw, task_open)
    except agntid.IntentPreparationError as exc:
        failure = TaskOpenFailure(
            stage="intent_unavailable",
            task_id=task_id,
            reason=str(exc),
            intent_reason=task_open.intent_reason,
        )
    if failure is not None:
        raise TaskOpenError(failure, cause_type="agntid.IntentPreparationError")

    names = {normalize_tool(t).name for t in shortlisted}
    selected = [t for t in visible if t.name in names]
    tools = tuple(t for t in selected if not t.is_runtime_control)
    excluded = tuple(t.name for t in selected if t.is_runtime_control)
    return tools, excluded


@asynccontextmanager
async def open_task_session(
    client: Any,
    config: ConnectorConfig,
    identity: TaskIdentity,
    prompt: str,
) -> AsyncIterator[TaskSession]:
    """Open an AgntID task and yield a TaskSession bound to it.

    Raises before yielding (so the block never runs and no business tool can be
    called) when identity or prompt is invalid, discovery fails, or the task is
    not opened (TaskOpenError with a TaskOpenFailure). The task context is
    exited, and task close sent, on every path after the task ID is created.
    `client` must already be connected; it is not closed here.
    """
    if not isinstance(identity, TaskIdentity):
        raise ConfigError("a TaskIdentity from trusted configuration is required", phase="identity")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ConfigError("task prompt must be a non-empty string", phase="task_create")

    visible_raw, visible = await _visible_tools(client, config)
    task_id = agntid.create_task(identity.agent_id, identity.user_id, prompt)
    # Only this wrapper adds the task argument; callers' reserved names were
    # already refused by TaskSession._rejection.
    wrapped = agntid.wrap_client(client, task_id, task_argument=TASK_ARGUMENT)

    session: TaskSession | None = None
    body_error: BaseException | None = None
    close_failure: ConnectorError | None = None
    try:
        async with agntid.task_context(client, task_id):
            try:
                task_open = await _open_task(client, config, identity, prompt, task_id)
                tools, excluded = _task_tools(visible_raw, visible, task_open, task_id)
                session = TaskSession(
                    task_id=task_id,
                    identity=identity,
                    tools=tools,
                    excluded_control_tools=excluded,
                    task_open=task_open,
                    wrapped=wrapped,
                    secret=config.access_token,
                )
                logger.info(
                    "task %s opened: %d visible tools, %d task tools",
                    task_id,
                    len(visible),
                    len(tools),
                )
                yield session
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                # Before task close is sent: no call can start after this point.
                if session is not None:
                    session._deactivate()
    except BaseException as exc:
        if exc is body_error or not isinstance(exc, Exception):
            raise
        # The exception came from task close (send_task_close in task_context).
        close_failure = classify_error(exc, phase="task_close", config=config)
    else:
        logger.info("task %s closed", task_id)
        return

    # Raised outside the except block so the transport exception is not chained.
    message = f"task close failed for task {task_id}: {close_failure.message}"
    logger.warning("%s", message)
    if session is not None:
        session.close_error = message
    if body_error is not None:
        body_error.add_note(message)
        raise body_error
    raise TaskCloseError(message, phase="task_close", cause_type=close_failure.cause_type)


__all__ = [
    "ENV_AGENT_ID",
    "ENV_USER_ID",
    "TASK_ARGUMENT",
    "CallStatus",
    "TaskCloseError",
    "TaskIdentity",
    "TaskOpenError",
    "TaskOpenFailure",
    "TaskSession",
    "TaskSessionClosedError",
    "TaskSessionError",
    "ToolCallResult",
    "is_reserved_argument",
    "load_identity",
    "open_task_session",
]
