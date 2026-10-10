"""AgntID runtime connector: configuration, connection, and tool discovery.

Uses only the documented AgntID Python SDK surface (source version 0.2.0):

    async with agntid.AgntidMCPClient(url, access_token=token) as client:
        tools = await agntid.get_tools_list(client)

This module reports what the runtime actually returns. A tool being *visible*
here does not mean it is authorized for any particular task: the runtime still
evaluates every call, and task-scoped shortlisting is a later module.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import agntid

ENV_MCP_URL = "AGNTID_MCP_URL"
ENV_ACCESS_TOKEN = "AGNTID_ACCESS_TOKEN"
ENV_TIMEOUT = "AGNTID_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 20.0
DOCUMENTED_LOCAL_MCP_URL = "http://localhost:8082/mcp"

# Runtime control tools defined by the SDK. They are reported (never hidden) but
# flagged, because later modules must never hand them to a model.
RUNTIME_CONTROL_TOOLS = frozenset(
    {
        agntid.TASK_OPEN_TOOL,
        agntid.TASK_CLOSE_TOOL,
        agntid.DELEGATION_OPEN_TOOL,
        agntid.DELEGATION_CLOSE_TOOL,
        agntid.ACTION_UPDATE_TOOL,
    }
)

_REDACTED = "[REDACTED]"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ConnectorError(Exception):
    """Base error. Messages are safe to print: they never contain the token."""

    kind = "error"
    exit_code = 1

    def __init__(self, message: str, *, phase: str, cause_type: str | None = None):
        super().__init__(message)
        self.message = message
        self.phase = phase
        self.cause_type = cause_type


class ConfigError(ConnectorError):
    kind = "configuration_error"
    exit_code = 2


class RuntimeUnreachableError(ConnectorError):
    kind = "runtime_unreachable"
    exit_code = 3


class AuthenticationError(ConnectorError):
    kind = "authentication_failed"
    exit_code = 4


class RuntimeProtocolError(ConnectorError):
    kind = "runtime_protocol_error"
    exit_code = 5


class DiscoveryError(ConnectorError):
    kind = "discovery_failed"
    exit_code = 6


class ConnectorTimeoutError(ConnectorError):
    kind = "timeout"
    exit_code = 7


class CleanupError(ConnectorError):
    kind = "cleanup_failed"
    exit_code = 8


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def redact_url(url: str) -> str:
    """Return the URL without user-info, query string, or fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "<unparseable URL>"


@dataclass(frozen=True)
class ConnectorConfig:
    mcp_url: str
    access_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def display_url(self) -> str:
        return redact_url(self.mcp_url)

    @property
    def uses_access_token(self) -> bool:
        return self.access_token is not None

    def __repr__(self) -> str:
        token = _REDACTED if self.access_token else None
        return (
            f"ConnectorConfig(mcp_url={self.display_url!r}, "
            f"access_token={token!r}, timeout_seconds={self.timeout_seconds!r})"
        )


def load_config(env: Mapping[str, str]) -> ConnectorConfig:
    """Build and validate connector configuration from an environment mapping.

    Raises ConfigError for missing or unsafe values. The endpoint is required:
    there is no silent fallback, so a misconfigured shell never quietly talks to
    a different runtime.
    """
    url = (env.get(ENV_MCP_URL) or "").strip()
    if not url:
        raise ConfigError(
            f"{ENV_MCP_URL} is not set. Set it to the runtime MCP address, for a "
            f"local runtime usually {DOCUMENTED_LOCAL_MCP_URL}",
            phase="config",
        )
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        raise ConfigError(f"{ENV_MCP_URL} is not a valid URL", phase="config") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            f"{ENV_MCP_URL} must be an http:// or https:// URL with a host "
            f"(got {redact_url(url)!r})",
            phase="config",
        )
    if parts.username or parts.password:
        raise ConfigError(
            f"{ENV_MCP_URL} must not embed credentials. Put the raw OAuth access "
            f"token in {ENV_ACCESS_TOKEN} instead",
            phase="config",
        )

    token = (env.get(ENV_ACCESS_TOKEN) or "").strip() or None
    if token is not None:
        if token.lower().startswith("bearer "):
            raise ConfigError(
                f"{ENV_ACCESS_TOKEN} must be the raw token without the 'Bearer' "
                "prefix; the SDK transport adds the scheme",
                phase="config",
            )
        if token == "oauth":
            # fastmcp interprets the literal string "oauth" as "start an
            # interactive OAuth browser flow", not as a bearer token.
            raise ConfigError(
                f"{ENV_ACCESS_TOKEN} must be an access token, not the literal 'oauth'",
                phase="config",
            )
        if any(ch.isspace() for ch in token):
            raise ConfigError(
                f"{ENV_ACCESS_TOKEN} contains whitespace; it must be a single raw token",
                phase="config",
            )

    raw_timeout = (env.get(ENV_TIMEOUT) or "").strip()
    timeout = DEFAULT_TIMEOUT_SECONDS
    if raw_timeout:
        try:
            timeout = float(raw_timeout)
        except ValueError:
            raise ConfigError(f"{ENV_TIMEOUT} must be a number of seconds", phase="config") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError(f"{ENV_TIMEOUT} must be a positive number of seconds", phase="config")

    return ConnectorConfig(mcp_url=url, access_token=token, timeout_seconds=timeout)


# --------------------------------------------------------------------------- #
# Tool metadata
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolInfo:
    """Readable view of one tool. `name` and `input_schema` are exactly as returned."""

    name: str
    description: str | None
    input_schema: dict[str, Any]
    title: str | None = None
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    is_runtime_control: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations,
            "isRuntimeControlTool": self.is_runtime_control,
        }


def _field(tool: Any, name: str) -> Any:
    if isinstance(tool, Mapping):
        return tool.get(name)
    return getattr(tool, name, None)


def _as_plain_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):  # pydantic models such as mcp.types.ToolAnnotations
        value = value.model_dump(exclude_none=True)
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    return None


def normalize_tool(tool: Any) -> ToolInfo:
    """Convert an SDK tool object (or dict) without altering name or schema."""
    name = _field(tool, "name")
    if not isinstance(name, str) or not name:
        raise DiscoveryError(
            "runtime returned a tool without a name; refusing to guess one",
            phase="discover",
        )
    input_schema = _as_plain_dict(_field(tool, "inputSchema"))
    if input_schema is None:
        raise DiscoveryError(
            f"runtime returned tool {name!r} without an object inputSchema",
            phase="discover",
        )
    description = _field(tool, "description")
    title = _field(tool, "title")
    return ToolInfo(
        name=name,
        description=description if isinstance(description, str) else None,
        input_schema=input_schema,
        title=title if isinstance(title, str) else None,
        output_schema=_as_plain_dict(_field(tool, "outputSchema")),
        annotations=_as_plain_dict(_field(tool, "annotations")),
        is_runtime_control=name in RUNTIME_CONTROL_TOOLS,
    )


@dataclass(frozen=True)
class DiscoveryResult:
    endpoint: str
    sdk_version: str
    authenticated: bool
    tools: tuple[ToolInfo, ...]

    @property
    def business_tools(self) -> tuple[ToolInfo, ...]:
        return tuple(t for t in self.tools if not t.is_runtime_control)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "sdkVersion": self.sdk_version,
            "accessTokenSupplied": self.authenticated,
            "toolCount": len(self.tools),
            "tools": [t.to_dict() for t in self.tools],
        }


def sdk_version() -> str:
    """Installed SDK version: the module constant plus the distribution metadata."""
    try:
        dist = importlib.metadata.version("agntid-sdk")
    except importlib.metadata.PackageNotFoundError:
        dist = "unknown"
    module = getattr(agntid, "__version__", "unknown")
    return module if module == dist else f"{module} (distribution {dist})"


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #


def _walk_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten an exception graph (cause, context, group members).

    fastmcp/anyio produce cyclic graphs (group <-> context), so a single shared
    visited set is required.
    """
    seen: set[int] = set()
    out: list[BaseException] = []
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        out.append(e)
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        stack.append(e.__cause__)
        stack.append(e.__context__)
    return out


def _scrub(text: str, secret: str | None) -> str:
    if secret:
        text = text.replace(secret, _REDACTED)
    return text


def classify_error(exc: BaseException, *, phase: str, config: ConnectorConfig) -> ConnectorError:
    """Map an SDK/transport exception to a token-free ConnectorError."""
    import httpx  # transport used by the SDK's fastmcp client
    from mcp.shared.exceptions import McpError

    endpoint = config.display_url
    chain = _walk_exceptions(exc)

    def first(kind: type[BaseException]) -> Any:
        return next((e for e in chain if isinstance(e, kind)), None)

    status_error = first(httpx.HTTPStatusError)
    if status_error is not None:
        status = status_error.response.status_code
        if status in (401, 403):
            hint = (
                "the supplied access token was rejected; confirm it is current, issued "
                "for the AgntID protected resource, and has no 'Bearer' prefix"
                if config.uses_access_token
                else f"the runtime requires client authentication; set {ENV_ACCESS_TOKEN} "
                "to a current raw OAuth access token"
            )
            return AuthenticationError(
                f"HTTP {status} from {endpoint}: {hint}",
                phase=phase,
                cause_type="httpx.HTTPStatusError",
            )
        return RuntimeProtocolError(
            f"HTTP {status} from {endpoint}; confirm {ENV_MCP_URL} points at the "
            "runtime's MCP endpoint (usually ending in /mcp)",
            phase=phase,
            cause_type="httpx.HTTPStatusError",
        )

    if first(httpx.TimeoutException) is not None or isinstance(exc, TimeoutError):
        return ConnectorTimeoutError(
            f"no response from {endpoint} within {config.timeout_seconds:g}s during {phase}",
            phase=phase,
            cause_type=type(exc).__name__,
        )

    connect_error = first(httpx.ConnectError) or first(httpx.NetworkError)
    if connect_error is not None:
        detail = _scrub(str(connect_error), config.access_token) or type(connect_error).__name__
        return RuntimeUnreachableError(
            f"cannot reach {endpoint} ({detail}); confirm the runtime is healthy "
            "(`docker compose ps`) and the MCP port is published",
            phase=phase,
            cause_type=f"httpx.{type(connect_error).__name__}",
        )

    mcp_error = first(McpError)
    if mcp_error is not None:
        detail = _scrub(str(mcp_error), config.access_token)
        if phase == "discover":
            return DiscoveryError(
                f"runtime rejected tools/list: {detail}",
                phase=phase,
                cause_type="mcp.McpError",
            )
        return RuntimeProtocolError(
            f"MCP session with {endpoint} failed ({detail}); confirm the URL is the "
            "runtime's MCP endpoint and MCP Support is enabled",
            phase=phase,
            cause_type="mcp.McpError",
        )

    detail = _scrub(str(exc), config.access_token) or type(exc).__name__
    error_cls = {"discover": DiscoveryError, "close": CleanupError}.get(phase, RuntimeProtocolError)
    return error_cls(
        f"{phase} failed against {endpoint}: {type(exc).__name__}: {detail}",
        phase=phase,
        cause_type=type(exc).__name__,
    )


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

ClientFactory = Callable[..., Any]


async def discover_tools(
    config: ConnectorConfig,
    *,
    client_factory: ClientFactory | None = None,
) -> DiscoveryResult:
    """Connect with the SDK client, list the visible tools, and close the session.

    The whole operation (connect, list, close) is bounded by
    `config.timeout_seconds` via asyncio cancellation; the SDK client exposes
    no timeout parameter of its own.
    """
    factory = client_factory or agntid.AgntidMCPClient
    phase = "connect"
    raw_tools: list[Any] = []
    error: ConnectorError | None = None
    try:
        async with asyncio.timeout(config.timeout_seconds):
            client = factory(config.mcp_url, access_token=config.access_token)
            async with client:
                phase = "discover"
                # get_tools_list returns [] for a client without list_tools/get_tools;
                # guard so that case can never masquerade as "runtime has no tools".
                if not callable(getattr(client, "list_tools", None)) and not callable(
                    getattr(client, "get_tools", None)
                ):
                    raise DiscoveryError(
                        "SDK client exposes neither list_tools nor get_tools",
                        phase=phase,
                    )
                raw_tools = await agntid.get_tools_list(client)
                phase = "close"
    except ConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a classified, token-free error
        error = classify_error(exc, phase=phase, config=config)
    if error is not None:
        # Raised outside the except block so the original exception, whose
        # httpx request object carries the Authorization header, is not chained.
        raise error

    tools = tuple(normalize_tool(t) for t in raw_tools)
    return DiscoveryResult(
        endpoint=config.display_url,
        sdk_version=sdk_version(),
        authenticated=config.uses_access_token,
        tools=tools,
    )
