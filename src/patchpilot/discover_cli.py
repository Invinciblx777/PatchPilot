"""Live discovery report: connect to the configured AgntID runtime and list its tools.

    patchpilot-discover            # human-readable report
    patchpilot-discover --json     # machine-readable report

Configuration comes from the process environment, with an optional .env file
filling in values that are not already set. Exit code 0 means the SDK listed
tools successfully (an empty list is still a success and is reported as empty).
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

from dotenv import dotenv_values  # noqa: E402

from patchpilot.agntid_connector import (  # noqa: E402
    ENV_ACCESS_TOKEN,
    ENV_MCP_URL,
    ENV_TIMEOUT,
    ConnectorError,
    DiscoveryResult,
    ToolInfo,
    discover_tools,
    load_config,
    redact_url,
    sdk_version,
)

CONFIG_KEYS = (ENV_MCP_URL, ENV_ACCESS_TOKEN, ENV_TIMEOUT)

VISIBILITY_NOTE = (
    "Visible is not authorized: the runtime filtered this list by protection profile, "
    "review state, tool settings, and role grants, and it still evaluates every call."
)


def merged_env(
    environ: Mapping[str, str],
    env_file: Path | None,
    keys: tuple[str, ...] = CONFIG_KEYS,
) -> tuple[dict[str, str], list[str]]:
    """Process environment wins; .env only fills keys that are unset or empty."""
    file_values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        file_values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    merged: dict[str, str] = {}
    sources: list[str] = []
    for key in keys:
        if environ.get(key, "").strip():
            merged[key] = environ[key]
            sources.append(f"{key}=environment")
        elif file_values.get(key, "").strip():
            merged[key] = file_values[key]
            sources.append(f"{key}={env_file}")
    return merged, sources


def _safe_text(value: str) -> str:
    """Neutralize terminal control sequences in runtime-supplied text."""
    return "".join(
        ch if ch in "\n\t" or (ch.isprintable() and ch != "\x1b") else f"\\x{ord(ch):02x}"
        for ch in value
    )


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def format_tool(index: int, tool: ToolInfo) -> str:
    lines = [f"[{index}] {_safe_text(tool.name)}"]
    if tool.is_runtime_control:
        lines.append("    kind:        AgntID runtime control tool (never expose to a model)")
    if tool.title:
        lines.append(f"    title:       {_safe_text(tool.title)}")
    if tool.description:
        lines.append("    description:")
        lines.append(_indent(_safe_text(tool.description.strip()), "      "))
    else:
        lines.append("    description: (none returned)")
    if tool.annotations:
        lines.append(f"    annotations: {_safe_text(json.dumps(tool.annotations, sort_keys=True))}")
    lines.append("    inputSchema:")
    lines.append(_indent(_safe_text(json.dumps(tool.input_schema, indent=2, sort_keys=True)), "      "))
    if tool.output_schema:
        lines.append("    outputSchema:")
        lines.append(_indent(_safe_text(json.dumps(tool.output_schema, indent=2, sort_keys=True)), "      "))
    return "\n".join(lines)


def format_report(result: DiscoveryResult, sources: list[str]) -> str:
    out = [
        "AgntID runtime discovery",
        f"  endpoint:      {result.endpoint}",
        f"  sdk:           agntid {result.sdk_version}",
        f"  access token:  {'supplied (not shown)' if result.authenticated else 'not supplied'}",
        f"  config:        {', '.join(sources)}",
        "  outcome:       OK - connected and tools/list succeeded via agntid.get_tools_list",
        f"  tools:         {len(result.tools)} returned "
        f"({len(result.business_tools)} business, "
        f"{len(result.tools) - len(result.business_tools)} runtime control)",
        f"  note:          {VISIBILITY_NOTE}",
        "",
    ]
    if not result.tools:
        out.append(
            "The runtime returned an EMPTY tool list. Check the protection profile review, "
            "tool classification, enabled state, role grants, and that Runtime sync shows Applied."
        )
    for i, tool in enumerate(result.tools, start=1):
        out.append(format_tool(i, tool))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def format_error(error: ConnectorError, endpoint: str | None, sources: list[str]) -> str:
    lines = [
        "AgntID runtime discovery",
        f"  endpoint:      {endpoint or '(not configured)'}",
        f"  sdk:           agntid {sdk_version()}",
        f"  config:        {', '.join(sources) or '(no AGNTID_* values found)'}",
        f"  outcome:       FAILED ({error.kind}) during {error.phase}",
        f"  error:         {_safe_text(error.message)}",
    ]
    if error.cause_type:
        lines.append(f"  cause type:    {error.cause_type}")
    lines.append("  No tools are reported because discovery did not complete.")
    return "\n".join(lines) + "\n"


def _error_json(error: ConnectorError, endpoint: str | None) -> dict[str, Any]:
    return {
        "ok": False,
        "endpoint": endpoint,
        "sdkVersion": sdk_version(),
        "error": {
            "kind": error.kind,
            "phase": error.phase,
            "message": error.message,
            "causeType": error.cause_type,
        },
    }


def run(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="patchpilot-discover",
        description="Connect to the configured AgntID runtime and list the tools it exposes.",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="dotenv file used for unset AGNTID_* values (default: ./.env)",
    )
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    env, sources = merged_env(os.environ if environ is None else environ, args.env_file)
    endpoint = redact_url(env[ENV_MCP_URL]) if env.get(ENV_MCP_URL, "").strip() else None
    try:
        config = load_config(env)
        result = asyncio.run(discover_tools(config))
    except ConnectorError as error:
        if args.json:
            stdout.write(json.dumps(_error_json(error, endpoint), indent=2) + "\n")
        else:
            stderr.write(format_error(error, endpoint, sources))
        return error.exit_code

    if args.json:
        stdout.write(json.dumps({"ok": True, **result.to_dict()}, indent=2) + "\n")
    else:
        stdout.write(format_report(result, sources))
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
