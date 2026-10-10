"""Serve the PatchPilot web UI on a loopback address.

    patchpilot-ui --repo ~/src/project --test-command "python -m pytest -q"
    patchpilot-ui --repo ~/src/a --repo ~/src/b --test-config tests.json --mapping capabilities.json

Repositories, test commands, the capability map, and sensitive-path
exceptions come only from these flags. The browser can pick a configured
repository and approve or decline the configured commands; it can never add a
path, a command, or a policy exception. AgntID settings (AGNTID_*,
PATCHPILOT_AGENT_ID, PATCHPILOT_USER_ID) are read from the environment and the
.env file for every operation, as the other commands do.

    PATCHPILOT_ACCESS_KEY=... patchpilot-ui --repo ~/src/project --public-origin https://ui.example.dev

--public-origin lets a hosted copy of the frontend at that origin drive this
server through a tunnel that forwards to the loopback listener with a loopback
Host header. It requires PATCHPILOT_ACCESS_KEY in the process environment;
every request must then carry it in X-PatchPilot-Access-Key (web/guard.py).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from patchpilot._warnings import silence_authlib_deprecations

silence_authlib_deprecations()

import uvicorn  # noqa: E402

import patchpilot.web  # noqa: E402
from patchpilot.patch_verification import (  # noqa: E402
    DEFAULT_TEST_TIMEOUT_SECONDS,
    TestConfigError,
    load_test_config,
    parse_test_command,
)
from patchpilot.patch_workspace import DEFAULT_SENSITIVE_PATTERNS, PatchPolicy  # noqa: E402
from patchpilot.web.app import create_app  # noqa: E402
from patchpilot.web.config import RepoEntry, UISettings  # noqa: E402

DEFAULT_PORT = 8765
STATIC_DIR = Path(patchpilot.web.__file__).parent / "static"
# Hosts the guard accepts in the Host header (web/guard.py). Binding anything
# else would expose test execution beyond this machine, so it is refused.
LOOPBACK_HOSTS = {"127.0.0.1": socket.AF_INET, "localhost": socket.AF_INET, "::1": socket.AF_INET6}
ACCESS_KEY_ENV = "PATCHPILOT_ACCESS_KEY"
MIN_ACCESS_KEY_LENGTH = 24


class UsageError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patchpilot-ui",
        description="Serve the PatchPilot web UI on a loopback address.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="loopback address to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT}; 0 picks a free one)")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv file (default: ./.env)")
    parser.add_argument("--mapping", type=Path, help="reviewed capability map (default: $PATCHPILOT_CAPABILITY_MAP)")
    parser.add_argument("--repo", action="append", default=[], type=Path, metavar="PATH",
                        help="local Git repository the UI may patch (repeatable)")
    parser.add_argument("--test-config", type=Path, help='JSON file: {"commands": [{"name", "argv", ...}]}')
    parser.add_argument("--test-command", action="append", default=[], metavar="CMD",
                        help="test command, split with shell rules but never run by a shell (repeatable)")
    parser.add_argument("--test-timeout", type=float, default=DEFAULT_TEST_TIMEOUT_SECONDS,
                        help="timeout in seconds for each --test-command (default: 600)")
    parser.add_argument("--allow-sensitive-path", action="append", default=[], metavar="PATTERN",
                        help="allow paths matching PATTERN despite the sensitive-path default deny")
    parser.add_argument("--public-origin", action="append", default=[], metavar="URL",
                        help=f"https origin of a hosted frontend that proxies /api here (repeatable; needs ${ACCESS_KEY_ENV})")
    return parser


def public_access(args: argparse.Namespace, environ: Mapping[str, str]) -> tuple[tuple[str, ...], str | None]:
    """Normalized --public-origin values and the access key they require."""
    origins: list[str] = []
    for raw in args.public_origin:
        parts = urlsplit(raw)
        if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise UsageError(f"--public-origin must be an https origin such as https://ui.example.dev; got {raw!r}")
        origins.append(f"https://{parts.netloc.lower()}")
    if not origins:
        return (), None
    key = environ.get(ACCESS_KEY_ENV, "")
    if len(key) < MIN_ACCESS_KEY_LENGTH:
        raise UsageError(
            f"--public-origin needs ${ACCESS_KEY_ENV} of at least {MIN_ACCESS_KEY_LENGTH} characters in the environment"
        )
    return tuple(dict.fromkeys(origins)), key


def build_settings(args: argparse.Namespace, environ: Mapping[str, str]) -> UISettings:
    repos: list[RepoEntry] = []
    for index, raw in enumerate(args.repo, start=1):
        if not raw.is_dir():
            raise UsageError(f"--repo {raw} is not a directory")
        path = raw.resolve()
        if any(r.path == path for r in repos):
            raise UsageError(f"--repo {raw} is listed more than once")
        repos.append(RepoEntry(id=f"r{index}", path=path, name=path.name))
    try:
        tests = list(load_test_config(args.test_config)) if args.test_config else []
        tests += [
            parse_test_command(command, timeout_seconds=args.test_timeout, index=i)
            for i, command in enumerate(args.test_command, start=len(tests) + 1)
        ]
    except TestConfigError as error:
        raise UsageError(error.message) from None
    return UISettings(
        environ=environ,
        env_file=args.env_file,
        mapping_path=args.mapping,
        repos=tuple(repos),
        tests=tuple(tests),
        policy=PatchPolicy(DEFAULT_SENSITIVE_PATTERNS, tuple(args.allow_sensitive_path)),
        static_dir=STATIC_DIR,
    )


def _listen(host: str, port: int) -> socket.socket:
    family = LOOPBACK_HOSTS.get(host)
    if family is None:
        raise UsageError(f"--host must be a loopback address ({', '.join(LOOPBACK_HOSTS)}); got {host!r}")
    if not 0 <= port <= 65535:
        raise UsageError("--port must be between 0 and 65535")
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1" if host == "localhost" else host, port))
        # Listen before the URL is printed, so an immediate request waits in the
        # backlog until uvicorn starts accepting instead of being refused.
        sock.listen(128)
    except OSError as error:
        sock.close()
        raise UsageError(f"cannot listen on {host}:{port}: {error.strerror}") from None
    return sock


def run(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    environ = os.environ if environ is None else environ
    try:
        settings = build_settings(args, environ)
        public_origins, access_key = public_access(args, environ)
        sock = _listen(args.host, args.port)
    except UsageError as error:
        stderr.write(f"patchpilot-ui: {error}\n")
        return 2
    port = sock.getsockname()[1]
    host = f"[{args.host}]" if ":" in args.host else ("127.0.0.1" if args.host == "localhost" else args.host)
    stdout.write(f"PatchPilot UI: http://{host}:{port}\n")
    if settings.tests:
        stdout.write("Test commands run repository code with your permissions; this is not an OS sandbox.\n")
    if public_origins:
        stdout.write(f"Public mode: {', '.join(public_origins)} may drive this server with the access key.\n")
        stdout.write(f"Tunnel to 127.0.0.1:{port} and send Host: 127.0.0.1:{port} upstream.\n")
    stdout.flush()
    app = create_app(settings, port=port, public_origins=public_origins, access_key=access_key)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
