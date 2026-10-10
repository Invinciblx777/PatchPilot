"""GitHub read capabilities: candidate discovery, reviewed mappings, and dispatch.

No GitHub tool name or schema is hardcoded here. The runtime decides which
tools exist and what they are called (an MCP server prefix may be part of the
name). PatchPilot only defines its own capability names and parameters:

    get_issue       owner, repo, issue_number
    get_repository  owner, repo
    read_file       owner, repo, path, ref (optional)

A capability becomes usable only through a human-reviewed mapping to one exact
discovered tool. The mapping pins the tool's inputSchema by SHA-256, so it can
only be written from a real inventory (patchpilot-github-preflight prints the
hashes). It is re-validated against the current task's tool metadata before
every call.

These states are kept separate and never collapsed:

    candidate        a tool whose metadata looks related (heuristic only)
    schema-verified  a reviewed mapping that matches the tool's real schema
    in task list     the tool is in this task's filtered list (Module 2)
    allowed          the runtime returned a result for a real call

Only the last one involves AgntID authorization. Mapping and argument checks
are PatchPilot application safeguards, not AgntID decisions.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import jsonschema

from patchpilot.agntid_connector import ConfigError, ToolInfo
from patchpilot.task_session import (
    CallStatus,
    TaskSession,
    ToolCallResult,
    is_reserved_argument,
)

ENV_CAPABILITY_MAP = "PATCHPILOT_CAPABILITY_MAP"


@dataclass(frozen=True)
class CapabilityParameter:
    name: str
    json_type: str  # "string" or "integer"
    required: bool = True


CAPABILITY_PARAMETERS: dict[str, tuple[CapabilityParameter, ...]] = {
    "get_issue": (
        CapabilityParameter("owner", "string"),
        CapabilityParameter("repo", "string"),
        CapabilityParameter("issue_number", "integer"),
    ),
    "get_repository": (
        CapabilityParameter("owner", "string"),
        CapabilityParameter("repo", "string"),
    ),
    "read_file": (
        CapabilityParameter("owner", "string"),
        CapabilityParameter("repo", "string"),
        CapabilityParameter("path", "string"),
        CapabilityParameter("ref", "string", required=False),
    ),
}


def schema_fingerprint(input_schema: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON form of a tool's inputSchema."""
    canonical = json.dumps(input_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Candidate discovery (heuristic; never proof of anything)
# --------------------------------------------------------------------------- #

_CANDIDATE_TOKENS = {
    "get_issue": frozenset({"issue", "issues"}),
    "get_repository": frozenset({"repo", "repos", "repository", "repositories"}),
    "read_file": frozenset({"file", "files", "content", "contents"}),
}
# A read capability is never suggested for a tool whose name reads like a write.
_WRITE_TOKENS = frozenset(
    {"create", "update", "delete", "remove", "add", "push", "merge", "write", "edit", "fork", "close", "comment"}
)


def _name_tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t}


def is_github_related(tool: ToolInfo) -> bool:
    """Heuristic: 'github' in the metadata text, or owner+repo input properties."""
    if tool.is_runtime_control:
        return False
    text = " ".join(filter(None, (tool.name, tool.title, tool.description))).lower()
    properties = tool.input_schema.get("properties")
    names = set(properties) if isinstance(properties, Mapping) else set()
    return "github" in text or {"owner", "repo"} <= names


def candidate_capabilities(tool: ToolInfo) -> tuple[str, ...]:
    if not is_github_related(tool):
        return ()
    tokens = _name_tokens(tool.name)
    if tokens & _WRITE_TOKENS:
        return ()
    return tuple(cap for cap, words in _CANDIDATE_TOKENS.items() if tokens & words)


def find_candidates(tools: Iterable[ToolInfo]) -> dict[str, tuple[ToolInfo, ...]]:
    """Candidate tools per capability: () is missing, more than one is ambiguous."""
    found: dict[str, list[ToolInfo]] = {cap: [] for cap in CAPABILITY_PARAMETERS}
    for tool in tools:
        for cap in candidate_capabilities(tool):
            found[cap].append(tool)
    return {cap: tuple(items) for cap, items in found.items()}


# --------------------------------------------------------------------------- #
# Reviewed mappings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapabilityMapping:
    capability: str
    tool: str
    schema_sha256: str
    arguments: Mapping[str, str]  # PatchPilot parameter -> exact tool argument name
    fixed_arguments: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""


_MAPPING_KEYS = {"tool", "schema_sha256", "arguments", "fixed_arguments"}


def parse_capability_map(data: Any, *, source: str) -> dict[str, CapabilityMapping]:
    """Check the structure of a capability map. Schema checks need live metadata."""

    def bad(message: str) -> ConfigError:
        return ConfigError(f"capability map {source}: {message}", phase="config")

    if not isinstance(data, Mapping) or set(data) != {"capabilities"}:
        raise bad('top level must be an object with exactly one key, "capabilities"')
    entries = data["capabilities"]
    if not isinstance(entries, Mapping):
        raise bad('"capabilities" must be an object')
    mappings: dict[str, CapabilityMapping] = {}
    for capability, entry in entries.items():
        if capability not in CAPABILITY_PARAMETERS:
            raise bad(f"unknown capability {capability!r}; known: {', '.join(CAPABILITY_PARAMETERS)}")
        if not isinstance(entry, Mapping):
            raise bad(f"{capability}: entry must be an object")
        unknown = set(entry) - _MAPPING_KEYS
        missing = {"tool", "schema_sha256", "arguments"} - set(entry)
        if unknown or missing:
            raise bad(f"{capability}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")
        tool, digest = entry["tool"], entry["schema_sha256"]
        if not isinstance(tool, str) or not tool:
            raise bad(f"{capability}: tool must be the exact, non-empty discovered tool name")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise bad(f"{capability}: schema_sha256 must be the 64-hex digest printed by the preflight")
        arguments = entry["arguments"]
        fixed = entry.get("fixed_arguments", {})
        if not isinstance(arguments, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in arguments.items()
        ):
            raise bad(f"{capability}: arguments must map parameter names to tool argument names")
        known = {p.name for p in CAPABILITY_PARAMETERS[capability]}
        if set(arguments) - known:
            raise bad(f"{capability}: unknown parameters {sorted(set(arguments) - known)}; known: {sorted(known)}")
        if not isinstance(fixed, Mapping) or not all(isinstance(k, str) for k in fixed):
            raise bad(f"{capability}: fixed_arguments must be an object")
        reserved = sorted({*filter(is_reserved_argument, arguments.values()), *filter(is_reserved_argument, fixed)})
        if reserved:
            raise bad(f"{capability}: {', '.join(map(repr, reserved))} is a reserved AgntID argument; "
                      "a mapping can never supply it")
        mappings[capability] = CapabilityMapping(
            capability=capability,
            tool=tool,
            schema_sha256=digest,
            arguments=dict(arguments),
            fixed_arguments=copy.deepcopy(dict(fixed)),
            source=source,
        )
    return mappings


def load_capability_map(path: Path) -> dict[str, CapabilityMapping]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read capability map {path}: {exc.strerror}", phase="config") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"capability map {path} is not valid JSON: {exc}", phase="config") from None
    return parse_capability_map(data, source=str(path))


@dataclass(frozen=True)
class MappingCheck:
    capability: str
    tool_name: str
    ok: bool
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "array" if isinstance(value, list) else "object"


def _declared_types(schema: Any, root: Mapping[str, Any], depth: int = 0) -> set[str] | None:
    """JSON types a property schema declares, or None if they cannot be determined."""
    if not isinstance(schema, Mapping) or depth > 8:
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str):
        match = re.fullmatch(r"#/(\$defs|definitions)/([^/]+)", ref)
        target = (root.get(match.group(1)) or {}).get(match.group(2)) if match else None
        return _declared_types(target, root, depth + 1)
    types: set[str] = set()
    declared = schema.get("type")
    if isinstance(declared, str):
        types.add(declared)
    elif isinstance(declared, list):
        types.update(t for t in declared if isinstance(t, str))
    for key in ("anyOf", "oneOf"):
        for member in schema.get(key) or ():
            member_types = _declared_types(member, root, depth + 1)
            if member_types is None:
                return None
            types |= member_types
    if "const" in schema:
        types.add(_json_type(schema["const"]))
    for value in schema.get("enum") or ():
        types.add(_json_type(value))
    return types or None


def _accepts(declared: set[str], json_type: str) -> bool:
    return json_type in declared or (json_type == "integer" and "number" in declared)


def _validator(schema: Mapping[str, Any]) -> jsonschema.protocols.Validator:
    cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    cls.check_schema(schema)
    return cls(schema)


def validate_mapping(
    mapping: CapabilityMapping, tools: Mapping[str, ToolInfo] | Iterable[ToolInfo]
) -> MappingCheck:
    """Check a reviewed mapping against real tool metadata. Never edits the mapping."""
    by_name = tools if isinstance(tools, Mapping) else {t.name: t for t in tools}
    problems: list[str] = []
    warnings: list[str] = []

    def result() -> MappingCheck:
        return MappingCheck(mapping.capability, mapping.tool, not problems, tuple(problems), tuple(warnings))

    tool = by_name.get(mapping.tool)
    if tool is None:
        problems.append(f"tool {mapping.tool!r} is not in the tool list it was checked against")
        return result()
    if tool.is_runtime_control:
        problems.append(f"{tool.name!r} is an AgntID runtime control tool, not a business tool")
        return result()
    actual = schema_fingerprint(tool.input_schema)
    if actual != mapping.schema_sha256:
        problems.append(
            f"inputSchema changed since review (mapping {mapping.schema_sha256[:12]}..., "
            f"tool {actual[:12]}...); review the new schema and update the mapping"
        )
    annotations = tool.annotations or {}
    if annotations.get("readOnlyHint") is False or annotations.get("destructiveHint") is True:
        problems.append("tool annotations declare it is not read-only; read capabilities need a read-only tool")
    elif annotations.get("readOnlyHint") is not True:
        warnings.append("tool does not declare readOnlyHint=true; confirm it is read-only before relying on it")

    schema = tool.input_schema
    try:
        validator = _validator(schema)
    except jsonschema.SchemaError as exc:
        problems.append(f"tool inputSchema is not a valid JSON Schema: {exc.message}")
        return result()
    if schema.get("type", "object") != "object":
        problems.append("tool inputSchema is not an object schema")
        return result()
    properties = schema.get("properties") or {}
    if not isinstance(properties, Mapping):
        problems.append("tool inputSchema has no usable properties")
        return result()

    params = {p.name: p for p in CAPABILITY_PARAMETERS[mapping.capability]}
    for param in params.values():
        if param.required and param.name not in mapping.arguments:
            problems.append(f"required parameter {param.name!r} is not mapped to a tool argument")

    covered: dict[str, bool] = {}  # tool argument -> always supplied?
    for param_name, target in mapping.arguments.items():
        param = params[param_name]
        if is_reserved_argument(target):
            problems.append(f"{param_name!r} maps to {target!r}, a reserved AgntID argument")
            continue
        if target in covered:
            problems.append(f"tool argument {target!r} is mapped more than once")
            continue
        if target not in properties:
            problems.append(f"{param_name!r} maps to {target!r}, which is not in the tool's inputSchema")
            continue
        declared = _declared_types(properties[target], schema)
        if declared is None:
            problems.append(f"tool argument {target!r} declares no type, so {param_name!r} cannot be verified")
        elif not _accepts(declared, param.json_type):
            problems.append(
                f"{param_name!r} is {param.json_type}, but tool argument {target!r} accepts "
                f"{', '.join(sorted(declared))}"
            )
        covered[target] = param.required

    partial = {k: v for k, v in schema.items() if k != "required"}
    partial_validator = type(validator)(partial)
    for name, value in mapping.fixed_arguments.items():
        if is_reserved_argument(name):
            problems.append(f"fixed argument {name!r} is a reserved AgntID argument")
        elif name in covered:
            problems.append(f"fixed argument {name!r} is also mapped from a parameter")
        elif name not in properties:
            problems.append(f"fixed argument {name!r} is not in the tool's inputSchema")
        else:
            errors = sorted(partial_validator.iter_errors({name: value}), key=lambda e: e.message)
            if errors:
                problems.append(f"fixed argument {name!r} does not match its schema: {errors[0].message}")
            covered[name] = True

    for required in schema.get("required") or ():
        if required not in covered:
            problems.append(f"required tool argument {required!r} is neither mapped nor fixed")
        elif covered[required] is False:
            problems.append(f"required tool argument {required!r} is mapped from an optional parameter")
    return result()


# --------------------------------------------------------------------------- #
# Execution through the Module 2 task session
# --------------------------------------------------------------------------- #


class OperationStatus(str, Enum):
    SUCCESS = "success"  # the runtime returned a tool result
    DENIED = "denied"  # the wrapped client returned an SDK DenialResult
    MISSING_CAPABILITY = "missing_capability"  # no reviewed mapping registered (nothing sent)
    NOT_IN_TASK = "not_in_task"  # mapped tool is not in this task's tool list (nothing sent)
    INVALID_MAPPING = "invalid_mapping"  # mapping does not match the tool schema (nothing sent)
    INVALID_ARGUMENTS = "invalid_arguments"  # request does not fit mapping/schema (nothing sent)
    REJECTED = "rejected"  # Module 2 dispatcher refused (nothing sent)
    EXECUTION_FAILED = "execution_failed"  # sent; the server answered with an error
    OUTCOME_UNKNOWN = "outcome_unknown"  # sent; no definite answer (timeout, transport); may have run


# Module 2 error type meaning the server answered with an error result: the call
# definitely failed. Everything else (McpError, which also covers JSON-RPC request
# timeouts, transport errors, unrecognized results) leaves the outcome unknown.
_DEFINITE_ERROR_TYPES = frozenset({"ToolError"})


@dataclass(frozen=True)
class CapabilityResult:
    capability: str
    status: OperationStatus
    task_id: str
    tool_name: str | None = None
    value: Any = None
    message: str = ""
    problems: tuple[str, ...] = ()
    denial: dict[str, Any] | None = None
    denial_explanation: str | None = None
    provenance: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status is OperationStatus.SUCCESS

    @property
    def call_sent(self) -> bool:
        return self.status in (
            OperationStatus.SUCCESS,
            OperationStatus.DENIED,
            OperationStatus.EXECUTION_FAILED,
            OperationStatus.OUTCOME_UNKNOWN,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "status": self.status.value,
            "callSent": self.call_sent,
            "taskId": self.task_id,
            "toolName": self.tool_name,
            "value": self.value,
            "message": self.message,
            "problems": list(self.problems),
            "denial": self.denial,
            "denialExplanation": self.denial_explanation,
            "provenance": self.provenance,
        }


def build_tool_arguments(
    mapping: CapabilityMapping, tool: ToolInfo, params: Any
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Translate capability parameters into exact tool arguments, or explain why not."""
    if not isinstance(params, Mapping):
        return None, ("parameters must be an object",)
    specs = {p.name: p for p in CAPABILITY_PARAMETERS[mapping.capability]}
    problems: list[str] = []
    for name in params:
        if name not in specs:
            problems.append(f"unknown parameter {name!r}")
        elif name not in mapping.arguments:
            problems.append(f"parameter {name!r} is not supported by the registered mapping")
    for spec in specs.values():
        if spec.name not in params:
            if spec.required:
                problems.append(f"missing required parameter {spec.name!r}")
            continue
        value = params[spec.name]
        if spec.json_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            problems.append(f"parameter {spec.name!r} must be an integer")
        if spec.json_type == "string" and (not isinstance(value, str) or not value):
            problems.append(f"parameter {spec.name!r} must be a non-empty string")
    if problems:
        return None, tuple(problems)

    arguments = copy.deepcopy(dict(mapping.fixed_arguments))
    for name, value in params.items():
        arguments[mapping.arguments[name]] = value
    # validate_mapping already refuses these; checked again on the final arguments.
    reserved = sorted(filter(is_reserved_argument, arguments))
    if reserved:
        return None, tuple(f"tool argument {name!r} is a reserved AgntID argument" for name in reserved)
    errors = sorted(_validator(tool.input_schema).iter_errors(arguments), key=lambda e: e.message)
    if errors:
        return None, tuple(f"tool inputSchema: {e.message}" for e in errors)
    return arguments, ()


async def execute_capability(
    session: TaskSession,
    mappings: Mapping[str, CapabilityMapping],
    capability: str,
    params: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> CapabilityResult:
    """Run one read capability through session.call_tool, exactly once.

    Never retries, never adjusts arguments after a denial or failure, and never
    uses a path other than the Module 2 task session.
    """

    def done(status: OperationStatus, **fields: Any) -> CapabilityResult:
        return CapabilityResult(capability=capability, status=status, task_id=session.task_id, **fields)

    if capability not in CAPABILITY_PARAMETERS:
        return done(OperationStatus.MISSING_CAPABILITY, message=f"unknown capability {capability!r}")
    mapping = mappings.get(capability)
    if mapping is None:
        return done(
            OperationStatus.MISSING_CAPABILITY,
            message=f"no reviewed mapping is registered for {capability!r}; the integration stays disabled",
        )
    tool = next((t for t in session.tools if t.name == mapping.tool), None)
    if tool is None:
        return done(
            OperationStatus.NOT_IN_TASK,
            tool_name=mapping.tool,
            message=f"{mapping.tool!r} is not in this task's tool list; nothing was sent",
        )
    check = validate_mapping(mapping, {tool.name: tool})
    if not check.ok:
        return done(
            OperationStatus.INVALID_MAPPING,
            tool_name=tool.name,
            message="the mapping does not match the task tool's schema; nothing was sent",
            problems=check.problems,
        )
    arguments, problems = build_tool_arguments(mapping, tool, params)
    if arguments is None:
        return done(
            OperationStatus.INVALID_ARGUMENTS,
            tool_name=tool.name,
            message="the request does not fit the mapping or the tool schema; nothing was sent",
            problems=problems,
        )

    provenance = {
        "taskId": session.task_id,
        "toolName": tool.name,
        "arguments": copy.deepcopy(arguments),
        "schemaSha256": mapping.schema_sha256,
        "mappingSource": mapping.source,
        "dispatchedVia": "TaskSession.call_tool -> agntid wrap_client call_tool_async",
    }
    try:
        async with asyncio.timeout(timeout_seconds):
            call = await session.call_tool(tool.name, arguments)
    except TimeoutError:
        return done(
            OperationStatus.OUTCOME_UNKNOWN,
            tool_name=tool.name,
            message=f"no answer within {timeout_seconds:g}s; the call may have run. It was not retried",
            provenance=provenance,
        )

    return done(operation_status(call), tool_name=tool.name, **_call_fields(call, provenance))


def operation_status(call: ToolCallResult) -> OperationStatus:
    """Map a Module 2 ToolCallResult to an operation status."""
    if call.status is CallStatus.SUCCESS:
        return OperationStatus.SUCCESS
    if call.status is CallStatus.DENIED:
        return OperationStatus.DENIED
    if call.status is CallStatus.REJECTED:
        return OperationStatus.REJECTED
    if call.error_type in _DEFINITE_ERROR_TYPES:
        return OperationStatus.EXECUTION_FAILED
    return OperationStatus.OUTCOME_UNKNOWN


def _call_fields(call: ToolCallResult, provenance: dict[str, Any]) -> dict[str, Any]:
    if call.status is CallStatus.SUCCESS:
        return {"value": call.value, "provenance": provenance}
    if call.status is CallStatus.REJECTED:  # nothing was sent
        return {"message": call.message}
    return {
        "message": call.message,
        "denial": call.denial,
        "denial_explanation": call.denial_explanation,
        "provenance": provenance,
    }


__all__ = [
    "CAPABILITY_PARAMETERS",
    "ENV_CAPABILITY_MAP",
    "CapabilityMapping",
    "CapabilityParameter",
    "CapabilityResult",
    "MappingCheck",
    "OperationStatus",
    "build_tool_arguments",
    "candidate_capabilities",
    "execute_capability",
    "find_candidates",
    "operation_status",
    "is_github_related",
    "load_capability_map",
    "parse_capability_map",
    "schema_fingerprint",
    "validate_mapping",
]
