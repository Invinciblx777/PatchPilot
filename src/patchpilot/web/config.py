"""Operator configuration for the web UI, and its read-only view.

Repositories, test commands, and the sensitive-path policy are fixed when the
server starts (patchpilot-ui flags). The browser can choose among them but can
never add one. AgntID settings are re-read from the process environment and
the .env file for every operation, exactly as each CLI run reads them, so a
configuration error is shown in the UI instead of preventing startup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchpilot import __version__
from patchpilot.agntid_connector import ConfigError, ConnectorError, load_config, sdk_version
from patchpilot.discover_cli import CONFIG_KEYS, merged_env
from patchpilot.github_capabilities import (
    CAPABILITY_PARAMETERS,
    ENV_CAPABILITY_MAP,
    CapabilityMapping,
    load_capability_map,
)
from patchpilot.patch_verification import TestCommand
from patchpilot.patch_workspace import PatchPolicy
from patchpilot.task_session import ENV_AGENT_ID, ENV_USER_ID, load_identity

ENV_KEYS = (*CONFIG_KEYS, ENV_AGENT_ID, ENV_USER_ID, ENV_CAPABILITY_MAP)


@dataclass(frozen=True)
class RepoEntry:
    """A local repository the operator allowed at startup. The browser sends only `id`."""

    id: str
    path: Path
    name: str


@dataclass(frozen=True)
class UISettings:
    environ: Mapping[str, str]
    env_file: Path | None
    mapping_path: Path | None  # --mapping; otherwise PATCHPILOT_CAPABILITY_MAP
    repos: tuple[RepoEntry, ...]
    tests: tuple[TestCommand, ...]
    policy: PatchPolicy
    static_dir: Path | None

    def merged_env(self) -> tuple[dict[str, str], list[str]]:
        """Current settings: process environment first, then the .env file."""
        return merged_env(self.environ, self.env_file, ENV_KEYS)

    def mapping_source(self, env: Mapping[str, str]) -> Path | None:
        if self.mapping_path is not None:
            return self.mapping_path
        value = (env.get(ENV_CAPABILITY_MAP) or "").strip()
        return Path(value) if value else None

    def load_mappings(self, env: Mapping[str, str]) -> dict[str, CapabilityMapping]:
        """The reviewed capability map, or {} when none is configured. Raises ConfigError."""
        path = self.mapping_source(env)
        return load_capability_map(path) if path is not None else {}

    def repo(self, repo_id: Any) -> RepoEntry | None:
        return next((r for r in self.repos if r.id == repo_id), None)


def error_view(error: ConnectorError) -> dict[str, Any]:
    return {"kind": error.kind, "phase": error.phase, "message": error.message}


def mapping_view(mapping: CapabilityMapping) -> dict[str, Any]:
    return {
        "tool": mapping.tool,
        "schemaSha256": mapping.schema_sha256,
        "arguments": dict(mapping.arguments),
        "fixedArguments": dict(mapping.fixed_arguments),
    }


def command_view(command: TestCommand) -> dict[str, Any]:
    return {
        "name": command.name,
        "argv": list(command.argv),
        "display": command.display(),
        "timeoutSeconds": command.timeout_seconds,
        "passEnv": list(command.pass_env),
    }


def config_view(settings: UISettings) -> dict[str, Any]:
    """Everything the Settings page shows. Never includes a secret value."""
    env, sources = settings.merged_env()
    view: dict[str, Any] = {
        "patchpilotVersion": __version__,
        "sdkVersion": sdk_version(),
        "envFile": None if settings.env_file is None else str(settings.env_file),
        "envFileFound": settings.env_file is not None and settings.env_file.is_file(),
        "sources": sources,
    }
    try:
        config = load_config(env)
        view["agntid"] = {
            "ok": True,
            "endpoint": config.display_url,
            "accessTokenSupplied": config.uses_access_token,
            "timeoutSeconds": config.timeout_seconds,
        }
    except ConfigError as error:
        view["agntid"] = {"ok": False, "error": error_view(error)}
    try:
        identity = load_identity(env)
        view["identity"] = {"ok": True, "agentId": identity.agent_id, "userId": identity.user_id}
    except ConfigError as error:
        view["identity"] = {"ok": False, "error": error_view(error)}

    path = settings.mapping_source(env)
    if path is None:
        view["capabilityMap"] = {"configured": False}
    else:
        try:
            mappings = load_capability_map(path)
            view["capabilityMap"] = {
                "configured": True,
                "path": str(path),
                "ok": True,
                "capabilities": {cap: mapping_view(m) for cap, m in mappings.items()},
            }
        except ConfigError as error:
            view["capabilityMap"] = {"configured": True, "path": str(path), "ok": False, "error": error.message}

    view["capabilityParameters"] = {
        cap: [{"name": p.name, "type": p.json_type, "required": p.required} for p in params]
        for cap, params in CAPABILITY_PARAMETERS.items()
    }
    view["repos"] = [{"id": r.id, "name": r.name, "path": str(r.path)} for r in settings.repos]
    view["tests"] = [command_view(t) for t in settings.tests]
    view["patchPolicy"] = {
        "sensitivePatterns": list(settings.policy.sensitive_patterns),
        "allowedSensitivePatterns": list(settings.policy.allowed_sensitive_patterns),
    }
    return view
