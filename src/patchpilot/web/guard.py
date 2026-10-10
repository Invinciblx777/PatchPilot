"""Local-only request guard and security headers for the web UI.

The UI backend runs operator-configured test commands, so a page on another
origin, or a DNS-rebinding page that reaches 127.0.0.1 under its own name,
must not be able to drive it. Every request is checked before routing:

    Host            must name this server on a loopback address
    /api/*          needs the per-process session header (except GET /api/session)
    Origin          if sent, must be this server's own origin
    Sec-Fetch-Site  if sent, must be same-origin or none
    POST            needs Content-Type: application/json (no CORS "simple request")

There is no CORS support at all: a cross-origin page can neither read
/api/session nor send the session header without a preflight, and a preflight
is never approved.

Public mode (patchpilot-ui --public-origin) is for a hosted frontend that
proxies /api to this server through a tunnel. The listener stays on loopback
and the tunnel must present a loopback Host header. Two things change: the
public origin is accepted in Origin, and every request must carry the
operator's access key in the X-PatchPilot-Access-Key header (the frontend asks
for it on its own sign-in screen). The access key is then the only barrier
between the internet and test execution.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_HEADER = "x-patchpilot-session"
SESSION_PATH = "/api/session"
ACCESS_HEADER = "x-patchpilot-access-key"

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
)


def loopback_hosts(port: int) -> frozenset[str]:
    """Host header values that address this server on a loopback interface."""
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


def _with_security_headers(send: Send, *, api: bool) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            for name, value in SECURITY_HEADERS:
                headers[name] = value
            if api:
                headers["cache-control"] = "no-store"
        await send(message)

    return wrapped


class LocalGuard:
    """ASGI middleware applying the checks above to every HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        port: int,
        token: str,
        public_origins: Iterable[str] = (),
        access_key: str | None = None,
    ):
        public_origins = frozenset(public_origins)
        if public_origins and not access_key:
            raise ValueError("a public origin needs an access key")
        self.app = app
        self.hosts = loopback_hosts(port)
        self.origins = frozenset(f"http://{host}" for host in self.hosts) | public_origins
        self._token = token.encode()
        self._access_key = access_key.encode() if access_key else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        api = path == "/api" or path.startswith("/api/")
        send = _with_security_headers(send, api=api)
        refusal = self._refusal(scope, Headers(scope=scope), api=api)
        if refusal is not None:
            status, code, message = refusal
            response = JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _refusal(self, scope: Scope, headers: Headers, *, api: bool) -> tuple[int, str, str] | None:
        if headers.get("host", "").lower() not in self.hosts:
            return 400, "host_not_allowed", "this PatchPilot server only answers requests addressed to its loopback address"
        if self._access_key is not None:
            supplied_key = headers.get(ACCESS_HEADER, "").encode()
            if not hmac.compare_digest(supplied_key, self._access_key):
                return 401, "access_key_required", "this PatchPilot server needs its access key"
        if not api:
            return None
        method: Any = scope["method"]
        if method == "OPTIONS":
            return 405, "method_not_allowed", "cross-origin requests are not supported"
        site = headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            return 403, "cross_site_request", "requests from other sites are refused"
        origin = headers.get("origin")
        if origin is not None and origin not in self.origins:
            return 403, "cross_origin_request", "requests from other origins are refused"
        if not (method in ("GET", "HEAD") and scope["path"] == SESSION_PATH):
            supplied = headers.get(SESSION_HEADER, "").encode()
            if not hmac.compare_digest(supplied, self._token):
                return 403, "session_required", "missing or wrong session header; reload the PatchPilot page"
        if method == "POST":
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return 415, "json_required", "requests must be sent as application/json"
        return None
