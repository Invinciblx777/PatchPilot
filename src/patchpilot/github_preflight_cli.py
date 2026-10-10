"""GitHub preflight: what the connected AgntID runtime actually offers for GitHub reads.

    patchpilot-github-preflight                         # inventory + mapping checks
    patchpilot-github-preflight --task-prompt "..."     # also open one task (Module 2)
    patchpilot-github-preflight --task-prompt "..." --run get_issue \\
        --arg owner=OWNER --arg repo=REPO --arg issue_number=N   # one real read

Everything printed comes from the connected endpoint. Nothing is assumed: a
capability is reported as schema-verified only when a reviewed mapping matches
a discovered tool's real schema, as in the task list only when the runtime's
task shortlist contains it, and as allowed only after a real --run call returned
a result. Without --run, no business tool is called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from patchpilot._warnings import silence_authlib_deprecations

silence_authlib_deprecations()

import agntid  # noqa: E402

from patchpilot.agntid_connector import (  # noqa: E402
    ConfigError,
    ConnectorConfig,
    ConnectorError,
    classify_error,
    discover_tools,
    load_config,
    redact_url,
    sdk_version,
)
from patchpilot.discover_cli import CONFIG_KEYS, _indent, _safe_text, merged_env  # noqa: E402
from patchpilot.github_capabilities import (  # noqa: E402
    CAPABILITY_PARAMETERS,
    ENV_CAPABILITY_MAP,
    CapabilityMapping,
    candidate_capabilities,
    execute_capability,
    find_candidates,
    is_github_related,
    load_capability_map,
    schema_fingerprint,
    validate_mapping,
)
from patchpilot.task_session import (  # noqa: E402
    ENV_AGENT_ID,
    ENV_USER_ID,
    TaskIdentity,
    TaskOpenError,
    load_identity,
    open_task_session,
)

EXIT_NO_VERIFIED_CAPABILITY = 9
EXIT_TASK_NOT_OPENED = 10
EXIT_RUN_NOT_SUCCESSFUL = 11

PORTAL_STEPS = (
    "Add a GitHub MCP server in the AgntID portal: Runtimes -> your runtime -> Operations -> MCP -> "
    "Servers -> Add MCP. Store its credential as an environment variable on the runtime host and "
    "refer to it by name (for example ${GITHUB_TOKEN}); never paste it into the form or into PatchPilot. "
    "Test Connection, Save MCP, wait for Apply: APPLIED, review its actions under Access Control -> "
    "Tools, and wait for Runtime sync: Applied."
)


def _parse_run_args(capability: str, pairs: list[str]) -> dict[str, Any]:
    specs = {p.name: p for p in CAPABILITY_PARAMETERS.get(capability, ())}
    if not specs:
        raise ConfigError(
            f"--run {capability!r} is not a PatchPilot capability; known: {', '.join(CAPABILITY_PARAMETERS)}",
            phase="config",
        )
    params: dict[str, Any] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or name not in specs:
            raise ConfigError(f"--arg must be NAME=VALUE with NAME in {sorted(specs)}", phase="config")
        if specs[name].json_type == "integer":
            try:
                params[name] = int(value, 10)
            except ValueError:
                raise ConfigError(f"--arg {name} must be an integer", phase="config") from None
        else:
            params[name] = value
    return params


async def _task_phase(
    config: ConnectorConfig,
    identity: TaskIdentity,
    prompt: str,
    mappings: Mapping[str, CapabilityMapping],
    run: tuple[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Open one Module 2 task; report its tool list; optionally run one read."""
    report: dict[str, Any] = {"opened": False}
    error: ConnectorError | None = None
    phase = "connect"
    try:
        async with agntid.AgntidMCPClient(config.mcp_url, access_token=config.access_token) as client:
            phase = "task"
            try:
                async with open_task_session(client, config, identity, prompt) as session:
                    report.update(
                        opened=True,
                        taskId=session.task_id,
                        toolNames=sorted(session.tool_names),
                        intentEngine=session.task_open.snapshot.engine_id if session.task_open.snapshot else "",
                        excludedControlTools=list(session.excluded_control_tools),
                    )
                    if run is not None:
                        result = await execute_capability(
                            session, mappings, run[0], run[1], timeout_seconds=config.timeout_seconds
                        )
                        report["run"] = result.to_dict()
            except TaskOpenError as failure:
                report["failure"] = {
                    "stage": failure.failure.stage,
                    "taskId": failure.failure.task_id,
                    "reason": failure.failure.reason,
                    "intentReason": failure.failure.intent_reason,
                }
    except ConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a classified, token-free error
        error = classify_error(exc, phase=phase, config=config)
    if error is not None:
        raise error
    return report


async def preflight(
    config: ConnectorConfig,
    mappings: Mapping[str, CapabilityMapping],
    *,
    identity: TaskIdentity | None = None,
    task_prompt: str | None = None,
    run: tuple[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    discovery = await discover_tools(config)  # Module 1
    visible = {t.name: t for t in discovery.tools}
    business = discovery.business_tools
    github_tools = [t for t in business if is_github_related(t)]
    candidates = find_candidates(business)

    task = None
    if task_prompt is not None and identity is not None:
        task = await _task_phase(config, identity, task_prompt, mappings, run)

    capabilities: dict[str, Any] = {}
    for cap in CAPABILITY_PARAMETERS:
        names = [t.name for t in candidates[cap]]
        entry: dict[str, Any] = {
            "candidates": names,
            "candidateState": "missing" if not names else "single" if len(names) == 1 else "ambiguous",
            "mapping": None,
            "listedByRuntime": None,
            "schemaCheck": None,
            "inTaskToolList": None,
            "runtimeAuthorization": "not tested (only --run makes a real call)",
        }
        mapping = mappings.get(cap)
        if mapping is not None:
            check = validate_mapping(mapping, visible)
            entry.update(
                mapping={"tool": mapping.tool, "source": mapping.source},
                listedByRuntime=mapping.tool in visible,
                schemaCheck={"ok": check.ok, "problems": list(check.problems), "warnings": list(check.warnings)},
            )
            if task is not None and task["opened"]:
                entry["inTaskToolList"] = mapping.tool in task["toolNames"]
        capabilities[cap] = entry

    run_report = task.get("run") if task else None
    if run_report is not None:
        if run_report["provenance"]:
            run_report["provenance"]["endpoint"] = config.display_url
        outcome = {
            "success": "allowed for this one call (the endpoint returned a result)",
            "denied": "denied for this one call (the endpoint returned a denial)",
        }.get(run_report["status"], f"not determined ({run_report['status']})")
        if run_report["capability"] in capabilities:
            capabilities[run_report["capability"]]["runtimeAuthorization"] = outcome

    report = {
        "endpoint": discovery.endpoint,
        "sdkVersion": discovery.sdk_version,
        "accessTokenSupplied": discovery.authenticated,
        "toolCount": len(discovery.tools),
        "businessToolCount": len(business),
        "githubRelatedTools": [
            {
                **t.to_dict(),
                "schemaSha256": schema_fingerprint(t.input_schema),
                "candidateFor": list(candidate_capabilities(t)),
            }
            for t in github_tools
        ],
        "capabilities": capabilities,
        "task": task,
    }
    report["nextSteps"] = _next_steps(report, mappings)
    return report


def _next_steps(report: dict[str, Any], mappings: Mapping[str, CapabilityMapping]) -> list[str]:
    steps: list[str] = []
    if not report["githubRelatedTools"]:
        steps.append("No usable GitHub integration was discovered on this runtime. " + PORTAL_STEPS)
    if report["githubRelatedTools"] and not mappings:
        steps.append(
            f"Write a capability map (see README) from the exact tool names, inputSchemas, and "
            f"schemaSha256 values above, then pass it with --mapping or {ENV_CAPABILITY_MAP}."
        )
    for cap, entry in report["capabilities"].items():
        if entry["mapping"] is None:
            continue
        if not entry["listedByRuntime"]:
            steps.append(
                f"{cap}: {entry['mapping']['tool']!r} is not listed. Check its review state, the protection "
                "profile, the tool's own setting, and role grants."
            )
        elif not entry["schemaCheck"]["ok"]:
            steps.append(f"{cap}: fix the mapping: {'; '.join(entry['schemaCheck']['problems'])}")
        elif entry["inTaskToolList"] is False:
            steps.append(
                f"{cap}: the runtime's shortlist for this prompt excluded {entry['mapping']['tool']!r}. "
                "That is the runtime's reading of the prompt; do not work around it."
            )
    task = report["task"]
    if task is None:
        steps.append("Re-run with --task-prompt to check the task-specific tool list.")
    elif not task["opened"]:
        failure = task["failure"]
        if failure["stage"] == "intent_unavailable":
            steps.append(
                "The task opened without an intent snapshot. Tool shortlisting needs LLM-based intent: "
                "portal -> Runtimes -> Operations -> Configuration -> Runtime features -> Intent engine."
            )
        else:
            steps.append(f"The task was not opened ({failure['stage']}); check runtime logs and Reporting.")
    elif "run" not in task and any(e["inTaskToolList"] for e in report["capabilities"].values()):
        steps.append("Use --run to make one real read; only a real call shows whether AgntID allows it.")
    return steps


def _exit_code(report: dict[str, Any]) -> int:
    task = report["task"]
    if task is not None and "run" in task:
        return 0 if task["run"]["status"] == "success" else EXIT_RUN_NOT_SUCCESSFUL
    if task is not None and not task["opened"]:
        return EXIT_TASK_NOT_OPENED
    verified = any(e["schemaCheck"] and e["schemaCheck"]["ok"] for e in report["capabilities"].values())
    return 0 if verified else EXIT_NO_VERIFIED_CAPABILITY


def _yes_no(value: bool | None, unknown: str) -> str:
    return unknown if value is None else "yes" if value else "NO"


def format_report(report: dict[str, Any]) -> str:
    out = [
        "PatchPilot GitHub preflight",
        f"  endpoint:      {report['endpoint']}",
        f"  sdk:           agntid {report['sdkVersion']}",
        f"  access token:  {'supplied (not shown)' if report['accessTokenSupplied'] else 'not supplied'}",
        "  connection:    OK - tools/list succeeded",
        f"  tools:         {report['toolCount']} listed, {report['businessToolCount']} business, "
        f"{len(report['githubRelatedTools'])} GitHub-related",
        "",
        "GitHub-related tools (heuristic match on name, description, or owner/repo inputs).",
        "These are CANDIDATES only: listed by the runtime, not verified, not authorized.",
    ]
    if not report["githubRelatedTools"]:
        out.append("  (none) No usable GitHub integration was discovered.")
    for i, tool in enumerate(report["githubRelatedTools"], start=1):
        out.append(f"[{i}] {_safe_text(tool['name'])}")
        out.append(f"    candidate for: {', '.join(tool['candidateFor']) or '(no read capability)'}")
        if tool["description"]:
            out.append("    description:")
            out.append(_indent(_safe_text(tool["description"].strip()), "      "))
        if tool["annotations"]:
            out.append(f"    annotations: {_safe_text(json.dumps(tool['annotations'], sort_keys=True))}")
        out.append(f"    schema_sha256: {tool['schemaSha256']}")
        out.append("    inputSchema:")
        out.append(_indent(_safe_text(json.dumps(tool["inputSchema"], indent=2, sort_keys=True)), "      "))
    out += ["", "Capabilities (PatchPilot names, not AgntID tool names)"]
    for cap, entry in report["capabilities"].items():
        out.append(f"  {cap}")
        names = ", ".join(_safe_text(n) for n in entry["candidates"]) or "(none)"
        out.append(f"    candidates:            {entry['candidateState']}: {names}")
        if entry["mapping"] is None:
            out.append("    mapping:               not registered (integration disabled)")
            continue
        out.append(
            f"    mapping:               {_safe_text(entry['mapping']['tool'])} "
            f"({_safe_text(entry['mapping']['source'])})"
        )
        out.append(f"    listed by runtime:     {_yes_no(entry['listedByRuntime'], '-')}")
        check = entry["schemaCheck"]
        out.append(f"    schema check:          {'verified' if check['ok'] else 'FAILED'}")
        for problem in check["problems"]:
            out.append(f"      problem: {_safe_text(problem)}")
        for warning in check["warnings"]:
            out.append(f"      warning: {_safe_text(warning)}")
        out.append(f"    in task tool list:     {_yes_no(entry['inTaskToolList'], 'not checked')}")
        out.append(f"    runtime authorization: {entry['runtimeAuthorization']}")
    task = report["task"]
    out += ["", "Task check (Module 2)"]
    if task is None:
        out.append("  not run (pass --task-prompt)")
    elif not task["opened"]:
        failure = task["failure"]
        out.append(f"  task NOT opened ({failure['stage']}): {_safe_text(failure['reason'])}")
        out.append("  No business tool was called.")
    else:
        out.append(f"  task {task['taskId']} opened; intent engine: {_safe_text(task['intentEngine'] or '(none)')}")
        out.append(f"  task tool list: {', '.join(_safe_text(n) for n in task['toolNames']) or '(empty)'}")
        run = task.get("run")
        if run is not None:
            out += _format_run(run)
    if report["nextSteps"]:
        out += ["", "Next steps"]
        out += [f"  - {_safe_text(step)}" for step in report["nextSteps"]]
    return "\n".join(out) + "\n"


def _format_run(run: dict[str, Any]) -> list[str]:
    lines = [f"  run {run['capability']}: {run['status'].upper()} (call sent: {'yes' if run['callSent'] else 'no'})"]
    if run["message"]:
        lines.append(f"    message: {_safe_text(run['message'])}")
    for problem in run["problems"]:
        lines.append(f"    problem: {_safe_text(problem)}")
    if run["denialExplanation"]:
        lines.append(_indent(_safe_text(run["denialExplanation"]), "    "))
    if run["status"] == "success":
        lines.append("    result (exactly as returned):")
        lines.append(_indent(_safe_text(json.dumps(run["value"], indent=2, default=str)), "      "))
    if run["provenance"]:
        lines.append("    provenance:")
        lines.append(_indent(_safe_text(json.dumps(run["provenance"], indent=2, sort_keys=True)), "      "))
    return lines


def format_error(error: ConnectorError, endpoint: str | None) -> str:
    return (
        "PatchPilot GitHub preflight\n"
        f"  endpoint:      {endpoint or '(not configured)'}\n"
        f"  outcome:       FAILED ({error.kind}) during {error.phase}\n"
        f"  error:         {_safe_text(error.message)}\n"
        "  No GitHub tools are reported because the runtime inventory was not obtained.\n"
    )


def run(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="patchpilot-github-preflight",
        description="Report which GitHub read capabilities the connected AgntID runtime really offers.",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv file (default: ./.env)")
    parser.add_argument("--mapping", type=Path, help=f"reviewed capability map (default: ${ENV_CAPABILITY_MAP})")
    parser.add_argument("--task-prompt", help="open one AgntID task with this prompt and check its tool list")
    parser.add_argument("--run", metavar="CAPABILITY", help="make one real read inside that task")
    parser.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE", help="parameter for --run")
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    keys = (*CONFIG_KEYS, ENV_AGENT_ID, ENV_USER_ID, ENV_CAPABILITY_MAP)
    env, _ = merged_env(os.environ if environ is None else environ, args.env_file, keys)
    endpoint = redact_url(env[CONFIG_KEYS[0]]) if env.get(CONFIG_KEYS[0], "").strip() else None
    try:
        config = load_config(env)
        mapping_path = args.mapping or (Path(env[ENV_CAPABILITY_MAP]) if env.get(ENV_CAPABILITY_MAP) else None)
        mappings = load_capability_map(mapping_path) if mapping_path else {}
        run_request = None
        if args.run is not None:
            if args.task_prompt is None:
                raise ConfigError("--run needs --task-prompt: every call happens inside an AgntID task", phase="config")
            run_request = (args.run, _parse_run_args(args.run, args.arg))
        elif args.arg:
            raise ConfigError("--arg is only used with --run", phase="config")
        identity = load_identity(env) if args.task_prompt is not None else None
        report = asyncio.run(preflight(config, mappings, identity=identity, task_prompt=args.task_prompt, run=run_request))
    except ConnectorError as error:
        if args.json:
            body = {"ok": False, "endpoint": endpoint, "sdkVersion": sdk_version(), "error": {
                "kind": error.kind, "phase": error.phase, "message": error.message}}
            stdout.write(json.dumps(body, indent=2) + "\n")
        else:
            stderr.write(format_error(error, endpoint))
        return error.exit_code

    code = _exit_code(report)
    if args.json:
        stdout.write(json.dumps({"ok": code == 0, **report}, indent=2, default=str) + "\n")
    else:
        stdout.write(format_report(report))
    return code


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
