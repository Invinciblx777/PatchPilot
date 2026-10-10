"""The operations behind the web UI, built only from the existing modules.

    runtime report   Module 1 discover_tools + Module 3 mapping checks
    investigation    Module 1 + Module 3 mapping checks + Module 2 task session
                     + Module 3 execute_capability (get_issue, read_file)
    task probe       Module 1 + Module 2 task session + at most one call_tool
    patch check/apply  Module 4 run_patch_workflow

Every business-tool call goes through TaskSession.call_tool (directly, or via
execute_capability). Nothing here uses the raw MCP client for a tool call,
talks to GitHub, or retries anything.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import agntid
import jsonschema

from patchpilot.agntid_connector import (
    ConfigError,
    ConnectorConfig,
    ConnectorError,
    ToolInfo,
    classify_error,
    discover_tools,
    load_config,
    sdk_version,
)
from patchpilot.github_capabilities import (
    CAPABILITY_PARAMETERS,
    CapabilityMapping,
    OperationStatus,
    execute_capability,
    find_candidates,
    is_github_related,
    operation_status,
    schema_fingerprint,
    validate_mapping,
)
from patchpilot.github_capabilities import _validator as schema_validator
from patchpilot.github_preflight_cli import PORTAL_STEPS
from patchpilot.patch_engine import Outcome, run_patch_workflow
from patchpilot.patch_verification import TestStatus
from patchpilot.patch_workspace import (
    GitError,
    InvalidRepositoryError,
    PatchPolicy,
    PatchRejectedError,
    open_repository,
    parse_patch,
)
from patchpilot.patch_workspace import _status_paths as status_paths
from patchpilot.task_session import (
    RESERVED_ARGUMENT_NAMES,
    RESERVED_ARGUMENT_PREFIX,
    TaskCloseError,
    TaskIdentity,
    TaskOpenError,
    TaskOpenFailure,
    TaskSession,
    is_reserved_argument,
    load_identity,
    open_task_session,
)
from patchpilot.web.config import RepoEntry, UISettings, error_view
from patchpilot.web.requests import FileRequest, InvestigationRequest, PatchRequest, ProbeCall, ProbeRequest
from patchpilot.web.runs import AGNTID, PATCHPILOT, Run, utc_now


def read_only_state(tool: ToolInfo) -> str:
    """What a tool's annotations declare: "declared", "declares_write", or "not_declared"."""
    annotations = tool.annotations or {}
    if annotations.get("readOnlyHint") is False or annotations.get("destructiveHint") is True:
        return "declares_write"
    if annotations.get("readOnlyHint") is True:
        return "declared"
    return "not_declared"


def tool_view(tool: ToolInfo) -> dict[str, Any]:
    return {
        **tool.to_dict(),
        "schemaSha256": schema_fingerprint(tool.input_schema),
        "readOnly": read_only_state(tool),
    }


def connector_error_view(error: ConnectorError) -> dict[str, Any]:
    return {**error_view(error), "causeType": error.cause_type}


def _load_map(settings: UISettings, env: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, CapabilityMapping]]:
    path = settings.mapping_source(env)
    if path is None:
        return {"configured": False}, {}
    try:
        return {"configured": True, "ok": True, "path": str(path)}, settings.load_mappings(env)
    except ConfigError as error:
        return {"configured": True, "ok": False, "path": str(path), "error": error.message}, {}


def _next_steps(github_tools: list[ToolInfo], map_state: dict[str, Any], capabilities: dict[str, Any]) -> list[str]:
    steps: list[str] = []
    if not github_tools:
        steps.append("No GitHub-related tool is listed by this runtime. " + PORTAL_STEPS)
    elif not map_state["configured"]:
        steps.append(
            "Write a reviewed capability map from the exact tool names and schema hashes above, then "
            "restart patchpilot-ui with --mapping FILE or set PATCHPILOT_CAPABILITY_MAP."
        )
    if map_state.get("ok") is False:
        steps.append(f"Fix the capability map: {map_state['error']}")
    for cap, entry in capabilities.items():
        if entry["mapping"] is None:
            continue
        if not entry["listedByRuntime"]:
            steps.append(
                f"{cap}: {entry['mapping']['tool']} is not listed. Check its review state, the protection "
                "profile, the tool's own setting, and role grants in the AgntID portal."
            )
        elif not entry["schemaCheck"]["ok"]:
            steps.append(f"{cap}: fix the mapping: {'; '.join(entry['schemaCheck']['problems'])}")
    return steps


async def runtime_report(settings: UISettings, run: Run) -> dict[str, Any]:
    """One live tools/list plus mapping checks. Never calls a tool."""
    started = time.monotonic()
    report: dict[str, Any] = {"checkedAt": utc_now(), "sdkVersion": sdk_version()}

    def finish(outcome: str, **fields: Any) -> dict[str, Any]:
        report.update(fields, durationMs=round((time.monotonic() - started) * 1000))
        run.finish(outcome, report)
        return report

    env, _ = settings.merged_env()
    try:
        config = load_config(env)
    except ConfigError as error:
        run.emit(PATCHPILOT, "config", "error", "AgntID connection is not configured", error_view(error))
        return finish(error.kind, ok=False, endpoint=None, error=connector_error_view(error))
    map_state, mappings = _load_map(settings, env)
    try:
        discovery = await discover_tools(config)
    except ConnectorError as error:
        run.emit(
            PATCHPILOT, "runtime", "error", f"Runtime check failed: {error.kind}",
            {**connector_error_view(error), "endpoint": config.display_url},
        )
        return finish(error.kind, ok=False, endpoint=config.display_url, error=connector_error_view(error))

    visible = {t.name: t for t in discovery.tools}
    business = discovery.business_tools
    candidates = find_candidates(business)
    github_tools = [t for t in business if is_github_related(t)]
    capabilities: dict[str, Any] = {}
    for cap in CAPABILITY_PARAMETERS:
        entry: dict[str, Any] = {
            "candidates": [t.name for t in candidates[cap]],
            "mapping": None,
            "listedByRuntime": None,
            "schemaCheck": None,
        }
        mapping = mappings.get(cap)
        if mapping is not None:
            check = validate_mapping(mapping, visible)
            entry.update(
                mapping={"tool": mapping.tool, "schemaSha256": mapping.schema_sha256, "source": mapping.source},
                listedByRuntime=mapping.tool in visible,
                schemaCheck={"ok": check.ok, "problems": list(check.problems), "warnings": list(check.warnings)},
            )
        capabilities[cap] = entry

    run.emit(
        AGNTID, "runtime", "ok", f"Runtime listed {len(discovery.tools)} tools ({len(business)} business)",
        {
            "endpoint": discovery.endpoint,
            "toolCount": len(discovery.tools),
            "businessToolCount": len(business),
            "toolNames": [t.name for t in discovery.tools],
        },
    )
    for cap, entry in capabilities.items():
        if entry["mapping"] is not None:
            ok = entry["schemaCheck"]["ok"]
            run.emit(
                PATCHPILOT, "mapping", "ok" if ok else "error",
                f"{cap}: mapping {'verified' if ok else 'failed'} (application check, not an AgntID decision)",
                {"capability": cap, **entry},
            )
    return finish(
        "connected",
        ok=True,
        endpoint=discovery.endpoint,
        accessTokenSupplied=discovery.authenticated,
        toolCount=len(discovery.tools),
        businessToolCount=len(business),
        tools=[tool_view(t) for t in discovery.tools],
        capabilities=capabilities,
        capabilityMap=map_state,
        nextSteps=_next_steps(github_tools, map_state, capabilities),
    )


# --------------------------------------------------------------------------- #
# Outcomes of real calls: who decided what
# --------------------------------------------------------------------------- #

# OperationStatus -> (event source, event status, AgntID decision, execution).
# Only a returned result counts as "allowed"; PatchPilot's own refusals never
# count as an AgntID decision, and a tool error claims no decision at all.
_OUTCOMES: dict[OperationStatus, tuple[str, str, str, str]] = {
    OperationStatus.SUCCESS: (AGNTID, "ok", "allowed", "success"),
    OperationStatus.DENIED: (AGNTID, "denied", "denied", "blocked"),
    OperationStatus.EXECUTION_FAILED: (AGNTID, "error", "not_determined", "failed"),
    OperationStatus.OUTCOME_UNKNOWN: (PATCHPILOT, "warning", "unknown", "unknown"),
    OperationStatus.NOT_IN_TASK: (PATCHPILOT, "warning", "not_observed", "not_sent"),
    OperationStatus.MISSING_CAPABILITY: (PATCHPILOT, "warning", "not_observed", "not_sent"),
    OperationStatus.INVALID_MAPPING: (PATCHPILOT, "error", "not_observed", "not_sent"),
    OperationStatus.INVALID_ARGUMENTS: (PATCHPILOT, "error", "not_observed", "not_sent"),
    OperationStatus.REJECTED: (PATCHPILOT, "error", "not_observed", "not_sent"),
}

_VERBS = {
    OperationStatus.SUCCESS: "allowed; the runtime returned a result",
    OperationStatus.DENIED: "denied by AgntID",
    OperationStatus.EXECUTION_FAILED: "sent; the tool answered with an error",
    OperationStatus.OUTCOME_UNKNOWN: "sent; no definite answer, so the call may have run (not retried)",
    OperationStatus.NOT_IN_TASK: "not in this task's tool list; nothing sent",
    OperationStatus.MISSING_CAPABILITY: "no reviewed mapping; nothing sent",
    OperationStatus.INVALID_MAPPING: "mapping does not match the tool schema; nothing sent",
    OperationStatus.INVALID_ARGUMENTS: "arguments do not fit the mapping or schema; nothing sent",
    OperationStatus.REJECTED: "refused by PatchPilot's dispatcher; nothing sent",
}


def _is_review(denial: Mapping[str, Any] | None) -> bool:
    return bool(denial) and (denial.get("outcome") == "review_required" or denial.get("review") is not None)


def emit_outcome(
    run: Run, step: str, label: str, status: OperationStatus, denial: Mapping[str, Any] | None, detail: dict[str, Any]
) -> None:
    source, event_status, decision, execution = _OUTCOMES[status]
    verb = _VERBS[status]
    if status is OperationStatus.DENIED and _is_review(denial):
        decision, verb = "review", "held for AgntID review"
    run.emit(source, step, event_status, f"{label}: {verb}", {**detail, "decision": decision, "execution": execution})


def _task_view(session: TaskSession) -> dict[str, Any]:
    snapshot = session.task_open.snapshot
    return {
        "taskId": session.task_id,
        "intentEngine": snapshot.engine_id if snapshot else "",
        "shortlist": sorted(session.tool_names),
        "excludedControlTools": list(session.excluded_control_tools),
        "relevantToolCount": session.task_open.relevant_tool_count,
    }


def _failure_event(run: Run, failure: TaskOpenFailure) -> None:
    # A rejection or a missing intent snapshot is the runtime's answer; a
    # task-open transport error is a failure PatchPilot observed.
    source = PATCHPILOT if failure.stage == "task_open_error" else AGNTID
    run.emit(
        source, "task", "error", f"Task not opened ({failure.stage}); no business tool was called",
        {
            "stage": failure.stage,
            "taskId": failure.task_id,
            "reason": failure.reason,
            "intentReason": failure.intent_reason,
            "note": "the SDK task context still sends task close for this task ID",
        },
    )


TaskBody = Callable[[TaskSession], Awaitable[str]]


async def run_in_task(run: Run, config: ConnectorConfig, identity: TaskIdentity, prompt: str, body: TaskBody) -> str:
    """Open one AgntID task (Module 2), run `body` inside it, and record open and close.

    Returns the body's outcome, or task_not_opened / runtime_error.
    """
    outcome = "runtime_error"
    error: ConnectorError | None = None
    phase = "connect"
    try:
        async with agntid.AgntidMCPClient(config.mcp_url, access_token=config.access_token) as client:
            phase = "task"
            task_id = ""
            try:
                async with open_task_session(client, config, identity, prompt) as session:
                    task_id = session.task_id
                    run.emit(AGNTID, "task", "ok", f"Task {task_id} opened", _task_view(session))
                    outcome = await body(session)
                run.emit(PATCHPILOT, "task", "info", f"Task close sent for {task_id}", {"taskId": task_id})
            except TaskOpenError as failure:
                _failure_event(run, failure.failure)
                outcome = "task_not_opened"
            except TaskCloseError as close_error:
                run.emit(PATCHPILOT, "task", "error", "Task close failed", {"taskId": task_id, "message": close_error.message})
    except ConnectorError as connector_error:
        error = connector_error
    except Exception as exc:  # noqa: BLE001 - re-raised as a classified, token-free error
        error = classify_error(exc, phase=phase, config=config)
    if error is not None:
        run.emit(PATCHPILOT, "runtime", "error", f"Connection to the runtime failed: {error.kind}", connector_error_view(error))
    return outcome


def _load_task_config(settings: UISettings, run: Run) -> tuple[ConnectorConfig, TaskIdentity, dict[str, str]] | None:
    env, _ = settings.merged_env()
    try:
        return load_config(env), load_identity(env), env
    except ConfigError as error:
        run.emit(PATCHPILOT, "config", "error", "Configuration error; nothing was sent", error_view(error))
        run.finish("config_error")
        return None


async def _discover(config: ConnectorConfig, run: Run) -> dict[str, ToolInfo] | None:
    try:
        discovery = await discover_tools(config)
    except ConnectorError as error:
        run.emit(
            PATCHPILOT, "runtime", "error", f"Runtime check failed: {error.kind}",
            {**connector_error_view(error), "endpoint": config.display_url},
        )
        run.finish("runtime_error")
        return None
    run.emit(
        AGNTID, "runtime", "ok", f"Runtime listed {len(discovery.tools)} tools ({len(discovery.business_tools)} business)",
        {
            "endpoint": discovery.endpoint,
            "toolCount": len(discovery.tools),
            "businessToolCount": len(discovery.business_tools),
            "toolNames": [t.name for t in discovery.tools],
        },
    )
    return {t.name: t for t in discovery.tools}


# --------------------------------------------------------------------------- #
# Investigation
# --------------------------------------------------------------------------- #

NO_ISSUE_MAPPING = (
    "Register a reviewed capability map with a get_issue entry (patchpilot-github-preflight prints "
    "the exact tool names and schema hashes), then restart patchpilot-ui with --mapping FILE or set "
    "PATCHPILOT_CAPABILITY_MAP."
)


async def investigate(settings: UISettings, run: Run, request: InvestigationRequest) -> None:
    """Read one issue, then any listed files, inside one AgntID task. Stops at the first non-success."""
    result: dict[str, Any] = {**request.view(), "task": None, "issueRead": None, "fileReads": []}
    loaded = _load_task_config(settings, run)
    if loaded is None:
        return
    config, identity, env = loaded
    try:
        mappings = settings.load_mappings(env)
    except ConfigError as error:
        run.emit(PATCHPILOT, "config", "error", "The capability map could not be loaded; nothing was sent", error_view(error))
        run.finish("config_error", result)
        return
    if "get_issue" not in mappings:
        run.emit(
            PATCHPILOT, "issue", "warning", "GitHub issue reads are unavailable: no reviewed capability map entry for get_issue",
            {"capability": "get_issue", "attempted": False, "nextStep": NO_ISSUE_MAPPING},
        )
        run.finish("unavailable", result)
        return
    visible = await _discover(config, run)
    if visible is None:
        return
    needed = ["get_issue"] + (["read_file"] if request.files and "read_file" in mappings else [])
    checks: dict[str, bool] = {}
    for cap in needed:
        check = validate_mapping(mappings[cap], visible)
        checks[cap] = check.ok
        run.emit(
            PATCHPILOT, "mapping", "ok" if check.ok else "error",
            f"{cap}: mapping {'verified' if check.ok else 'failed'} (application check, not an AgntID decision)",
            {
                "capability": cap,
                "tool": mappings[cap].tool,
                "listedByRuntime": mappings[cap].tool in visible,
                "schemaCheck": {"ok": check.ok, "problems": list(check.problems), "warnings": list(check.warnings)},
            },
        )
    if not checks["get_issue"]:
        run.finish("invalid_mapping", result)
        return

    async def reads(session: TaskSession) -> str:
        result["task"] = _task_view(session)
        issue = await execute_capability(
            session, mappings, "get_issue",
            {"owner": request.issue.owner, "repo": request.issue.repo, "issue_number": request.issue.number},
            timeout_seconds=config.timeout_seconds,
        )
        result["issueRead"] = issue.to_dict()
        emit_outcome(run, "issue", f"get_issue {request.issue.reference}", issue.status, issue.denial,
                     {**issue.to_dict(), "issue": request.issue.view()})
        if not request.files:
            return issue.status.value
        if not issue.ok:
            _not_attempted(run, request.files, "the run stops after the issue read did not succeed")
            return issue.status.value
        if "read_file" not in mappings:
            run.emit(PATCHPILOT, "file", "warning", "File reads are unavailable: no reviewed capability map entry for read_file",
                     {"capability": "read_file", "attempted": False, "paths": [f.path for f in request.files]})
            return "files_unavailable"
        if not checks["read_file"]:
            _not_attempted(run, request.files, "the read_file mapping does not match the tool schema")
            return "files_unavailable"
        for index, file in enumerate(request.files):
            params = {"owner": request.issue.owner, "repo": request.issue.repo, "path": file.path}
            if file.ref is not None:
                params["ref"] = file.ref
            read = await execute_capability(session, mappings, "read_file", params, timeout_seconds=config.timeout_seconds)
            result["fileReads"].append({**read.to_dict(), **file.view()})
            emit_outcome(run, "file", f"read_file {file.path}", read.status, read.denial, {**read.to_dict(), **file.view()})
            if not read.ok:
                _not_attempted(run, request.files[index + 1:], "the run stops after a read that did not succeed")
                return read.status.value
        return "success"

    outcome = await run_in_task(run, config, identity, request.prompt, reads)
    run.finish(outcome, result)


def _not_attempted(run: Run, files: tuple[FileRequest, ...], reason: str) -> None:
    if files:
        run.emit(PATCHPILOT, "file", "info", f"File reads not attempted: {reason}",
                 {"attempted": False, "reason": reason, "paths": [f.path for f in files]})


# --------------------------------------------------------------------------- #
# Task probe
# --------------------------------------------------------------------------- #


def probe_problems(tool: ToolInfo | None, call: ProbeCall) -> list[str]:
    """Why a probe call must not be sent (application checks, not AgntID decisions)."""
    if tool is None:
        return [f"{call.tool!r} is not listed by the runtime"]
    if tool.is_runtime_control:
        return [f"{tool.name!r} is an AgntID runtime control tool; the probe never calls it"]
    problems: list[str] = []
    state = read_only_state(tool)
    if state == "declares_write":
        problems.append("the tool's annotations declare it is not read-only; the probe only calls read tools")
    elif state == "not_declared" and not call.confirm_unannotated:
        problems.append("the tool does not declare readOnlyHint=true; confirm the call explicitly before it is sent")
    reserved = sorted(k for k in call.arguments if k in RESERVED_ARGUMENT_NAMES or k.startswith(RESERVED_ARGUMENT_PREFIX))
    if reserved:
        problems.append(f"argument {', '.join(reserved)} is reserved for AgntID task correlation")
    # Live runtime schemas also list agntid_task_id, agntid_delegation_id, and
    # agntid_approval. Those are the runtime's to fill; a caller never may.
    managed = sorted(k for k in call.arguments if is_reserved_argument(k) and k not in reserved)
    if managed:
        problems.append(f"argument {', '.join(managed)} is runtime-managed (agntid_*); AgntID fills it, never the caller")
    if reserved or managed:
        return problems  # schema errors quote values, and a reserved value may be approval material
    try:
        validator = schema_validator(tool.input_schema)
    except jsonschema.SchemaError as exc:
        problems.append(f"the tool's inputSchema is not a valid JSON Schema: {exc.message}")
    else:
        errors = sorted(validator.iter_errors(dict(call.arguments)), key=lambda e: e.message)
        problems.extend(f"inputSchema: {e.message}" for e in errors)
    return problems


async def probe(settings: UISettings, run: Run, request: ProbeRequest) -> None:
    """Open one task for a prompt; optionally call one shortlisted tool exactly once."""
    result: dict[str, Any] = {**request.view(), "task": None, "call": None}
    loaded = _load_task_config(settings, run)
    if loaded is None:
        return
    config, identity, _ = loaded
    visible = await _discover(config, run)
    if visible is None:
        return
    call = request.call
    if call is not None:
        tool = visible.get(call.tool)
        problems = probe_problems(tool, call)
        if problems:
            run.emit(PATCHPILOT, "probe_check", "error", "Call refused before opening a task; nothing was sent",
                     {"tool": call.tool, "problems": problems})
            run.finish("refused", result)
            return
        state = read_only_state(tool)
        run.emit(
            PATCHPILOT, "probe_check", "ok" if state == "declared" else "warning",
            "Call checks passed (application check, not an AgntID decision)",
            {"tool": tool.name, "readOnly": state, "confirmed": call.confirm_unannotated,
             "schemaSha256": schema_fingerprint(tool.input_schema)},
        )

    async def body(session: TaskSession) -> str:
        result["task"] = _task_view(session)
        if call is None:
            return "task_opened"
        task_tool = next((t for t in session.tools if t.name == call.tool), None)
        if task_tool is not None and (problems := probe_problems(task_tool, call)):
            run.emit(PATCHPILOT, "probe_check", "error", "Call refused against the task's tool metadata; nothing was sent",
                     {"tool": call.tool, "problems": problems})
            return "refused"
        detail: dict[str, Any] = {"tool": call.tool, "arguments": call.arguments_view(), "taskId": session.task_id}
        try:
            async with asyncio.timeout(config.timeout_seconds):
                outcome = await session.call_tool(call.tool, call.arguments)
        except TimeoutError:
            status = OperationStatus.OUTCOME_UNKNOWN
            detail.update(status=status.value, callSent=True,
                          message=f"no answer within {config.timeout_seconds:g}s; the call may have run. It was not retried")
            emit_outcome(run, "call", call.tool, status, None, detail)
            result["call"] = detail
            return status.value
        status = operation_status(outcome)
        detail.update(
            status=outcome.status.value, callSent=outcome.call_sent, value=outcome.value, message=outcome.message,
            denial=outcome.denial, denialExplanation=outcome.denial_explanation, errorType=outcome.error_type,
        )
        emit_outcome(run, "call", call.tool, status, outcome.denial, detail)
        result["call"] = detail
        return status.value

    outcome = await run_in_task(run, config, identity, request.prompt, body)
    run.finish(outcome, result)


# --------------------------------------------------------------------------- #
# Patch check and apply (Module 4; local only)
# --------------------------------------------------------------------------- #

_PATCH_EVENTS: dict[Outcome, tuple[str, str]] = {
    Outcome.CHECK_PASSED: ("ok", "Patch applies cleanly to the base; nothing was applied (PatchPilot check)"),
    Outcome.TESTS_PASSED: ("ok", "Patch applied in a disposable worktree; every configured test command passed"),
    Outcome.PATCH_APPLIED: ("info", "Patch applied in a disposable worktree; no tests were run"),
    Outcome.NOT_APPLIED: ("info", "Patch not applied (declined)"),
    Outcome.TESTS_FAILED: ("error", "Patch applied; tests failed, so this is not a successful fix"),
    Outcome.TESTS_INCOMPLETE: ("warning", "Patch applied; tests are incomplete (timed out or could not start)"),
    Outcome.UNCOMMITTED_CHANGES: ("warning", "Refused: the repository has uncommitted changes; nothing was done"),
    Outcome.INVALID_REPOSITORY: ("error", "Invalid repository; nothing was done"),
    Outcome.INVALID_PATCH: ("error", "Invalid patch; nothing was applied"),
    Outcome.UNSAFE_PATCH: ("error", "Unsafe patch rejected by PatchPilot's path rules; nothing was applied"),
    Outcome.PATCH_CHECK_FAILED: ("error", "The patch does not apply to the base; nothing was applied"),
    Outcome.WORKSPACE_ERROR: ("error", "Workspace error"),
}

_TEST_EVENTS = {
    TestStatus.PASSED: "ok",
    TestStatus.FAILED: "error",
    TestStatus.TIMED_OUT: "warning",
    TestStatus.NOT_STARTED: "error",
}


def patch_digest(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def file_changes(patch: str, policy: PatchPolicy) -> list[dict[str, Any]] | None:
    """Structured changes from the same strict parser Module 4 uses, or None if it rejects the patch."""
    try:
        proposal = parse_patch(patch, policy)
    except PatchRejectedError:
        return None
    return [
        {
            "kind": f.kind, "oldPath": f.old_path, "newPath": f.new_path,
            "added": f.added, "removed": f.removed, "oldMode": f.old_mode, "newMode": f.new_mode,
        }
        for f in proposal.files
    ]


async def patch_workflow(settings: UISettings, run: Run, request: PatchRequest, repo: RepoEntry, *, apply: bool) -> None:
    """Run Module 4 once in a worker thread. Approvals are exactly the request's two flags."""
    loop = asyncio.get_running_loop()
    digest = patch_digest(request.patch)
    verb = "Applying" if apply else "Checking"
    run.emit(PATCHPILOT, "patch", "info", f"{verb} patch against {repo.name} at {request.base}",
             {"repoId": repo.id, "base": request.base, "patchSha256": digest, "checkOnly": not apply})

    def approve(stage: str, detail: str) -> bool:
        approved = request.approve_apply if stage == "apply" else request.approve_tests
        if stage == "apply":
            title = "Apply approved by the operator; applying in a disposable worktree"
        else:
            title = ("Test run approved by the operator; running configured commands" if approved
                     else "Test run declined by the operator")
        loop.call_soon_threadsafe(
            functools.partial(run.emit, PATCHPILOT, stage, "info", title, {"approved": approved, "detail": detail})
        )
        return approved

    result = await asyncio.to_thread(
        run_patch_workflow,
        repo.path,
        request.base,
        request.patch,
        approve=approve,
        policy=settings.policy,
        tests=settings.tests,
        allow_uncommitted=request.allow_uncommitted,
        check_only=not apply,
    )
    for test in result.tests:
        if test.status in _TEST_EVENTS:
            took = "" if test.duration_seconds is None else f", {test.duration_seconds:g}s"
            code = "" if test.exit_code is None else f", exit code {test.exit_code}"
            run.emit(PATCHPILOT, "tests", _TEST_EVENTS[test.status], f"{test.name}: {test.status.value}{code}{took}",
                     {k: v for k, v in test.to_dict().items() if k != "output"})
    if result.cleanup is not None and result.cleanup.status == "failed":
        run.emit(PATCHPILOT, "cleanup", "error", "Workspace cleanup failed", {"errors": list(result.cleanup.errors)})
    if result.original_preserved is False:
        run.emit(PATCHPILOT, "cleanup", "error", "The original working tree changed unexpectedly", {})
    status, title = _PATCH_EVENTS[result.outcome]
    run.emit(PATCHPILOT, "patch", status, title, {
        "outcome": result.outcome.value,
        "problems": list(result.problems),
        "files": list(result.files),
        "baseSha": result.base_sha,
        "changedPaths": list(result.changed_paths),
        "cleanup": None if result.cleanup is None else result.cleanup.status,
        "originalPreserved": result.original_preserved,
    })
    run.finish(result.outcome.value, {
        **result.to_dict(),
        "repoId": repo.id,
        "repoName": repo.name,
        "patchSha256": digest,
        "fileChanges": file_changes(request.patch, settings.policy),
        "testsConfigured": len(settings.tests),
    })


def repository_view(repo: RepoEntry) -> dict[str, Any]:
    """Live, read-only snapshot of one configured repository (rev-parse, status, symbolic-ref)."""
    view: dict[str, Any] = {"id": repo.id, "name": repo.name, "path": str(repo.path)}
    try:
        snapshot = open_repository(repo.path).snapshot
    except (InvalidRepositoryError, GitError) as error:
        return {**view, "ok": False, "error": error.message}
    return {
        **view,
        "ok": True,
        "branch": snapshot.branch,
        "head": snapshot.head,
        "dirty": snapshot.dirty,
        "changedPaths": len(set(status_paths(snapshot.status))),
    }
