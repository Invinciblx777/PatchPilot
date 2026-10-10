"""Silence two known third-party deprecation messages emitted on SDK import.

fastmcp (the AgntID SDK's MCP client) imports authlib, which emits these
warnings at import time and installs an "always" filter for its own warning
class. Our filters must be added after authlib's module is loaded so they take
precedence. Only these exact messages are suppressed.
"""

from __future__ import annotations

import warnings

_MESSAGES = (
    "authlib.jose module is deprecated",
    "The httpx module is deprecated",
)


def silence_authlib_deprecations() -> None:
    try:
        import authlib.deprecate  # noqa: F401  (installs authlib's own "always" filter)
    except ImportError:
        return
    for message in _MESSAGES:
        warnings.filterwarnings("ignore", message=message)
