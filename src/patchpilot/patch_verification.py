"""Run operator-configured test commands in a patch workspace and report real outcomes.

Commands come only from trusted operator configuration (a JSON file or
explicit command-line flags). They are never read from the repository, the
patch, an issue, or model output.

Running a repository's tests runs repository code. It runs in the isolated
worktree with a minimal environment and a temporary HOME, but this is NOT an
operating-system sandbox: the process has the operator's user permissions and
network access.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_TEST_TIMEOUT_SECONDS = 600.0
MAX_OUTPUT_CHARS = 20_000
_REDACTED = "[REDACTED]"

# Variables a test command always gets. Anything else must be named in pass_env.
_BASE_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
# Never passed, even if named in pass_env.
_NEVER_PASS = frozenset({"AGNTID_ACCESS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"})
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY", re.IGNORECASE)


class TestConfigError(Exception):
    __test__ = False  # not a pytest test class

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class TestCommand:
    __test__ = False  # not a pytest test class

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS
    pass_env: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise TestConfigError("a test command needs a non-empty name")
        if not self.argv or not all(isinstance(a, str) and a for a in self.argv):
            raise TestConfigError(f"test command {self.name!r} needs a non-empty argv of strings")
        if not isinstance(self.timeout_seconds, (int, float)) or not 0 < self.timeout_seconds <= 86_400:
            raise TestConfigError(f"test command {self.name!r} needs a timeout between 0 and 86400 seconds")
        blocked = sorted(set(self.pass_env) & _NEVER_PASS)
        if blocked:
            raise TestConfigError(f"test command {self.name!r} may not receive {', '.join(blocked)}")
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in self.pass_env):
            raise TestConfigError(f"test command {self.name!r}: pass_env must list variable names")

    def display(self) -> str:
        return shlex.join(self.argv)


def parse_test_command(text: str, *, timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS, index: int = 1) -> TestCommand:
    """Split an operator-typed command into argv (shlex rules). It is never run by a shell."""
    try:
        argv = tuple(shlex.split(text))
    except ValueError as exc:
        raise TestConfigError(f"cannot parse test command {text!r}: {exc}") from None
    return TestCommand(name=f"command-{index}", argv=argv, timeout_seconds=timeout_seconds)


def load_test_config(path: Path) -> tuple[TestCommand, ...]:
    """Read {"commands": [{"name", "argv", "timeout_seconds"?, "pass_env"?}, ...]}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TestConfigError(f"cannot read test config {path}: {exc.strerror}") from None
    except ValueError as exc:
        raise TestConfigError(f"test config {path} is not valid JSON: {exc}") from None
    if not isinstance(data, Mapping) or set(data) != {"commands"} or not isinstance(data["commands"], list):
        raise TestConfigError(f'test config {path} must be {{"commands": [...]}}')
    commands = []
    for entry in data["commands"]:
        if not isinstance(entry, Mapping) or not {"name", "argv"} <= set(entry) <= {
            "name", "argv", "timeout_seconds", "pass_env"
        }:
            raise TestConfigError(f"test config {path}: each command needs name and argv (and nothing unknown)")
        if not isinstance(entry["argv"], list) or not isinstance(entry.get("pass_env", []), list):
            raise TestConfigError(f"test config {path}: argv and pass_env must be lists")
        commands.append(
            TestCommand(
                name=entry["name"],
                argv=tuple(entry["argv"]),
                timeout_seconds=entry.get("timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS),
                pass_env=tuple(entry.get("pass_env", [])),
            )
        )
    if len({c.name for c in commands}) != len(commands):
        raise TestConfigError(f"test config {path}: command names must be unique")
    return tuple(commands)


class TestStatus(str, Enum):
    __test__ = False

    PASSED = "passed"  # exit code 0
    FAILED = "failed"  # non-zero exit code (including signals)
    TIMED_OUT = "timed_out"  # killed at the timeout; results are incomplete
    NOT_STARTED = "not_started"  # the program could not be started
    SKIPPED = "skipped"  # the operator declined to run it
    NOT_RUN = "not_run"  # a precondition failed (for example, the patch did not apply)


@dataclass(frozen=True)
class TestRun:
    __test__ = False

    name: str
    argv: tuple[str, ...]
    status: TestStatus
    exit_code: int | None = None
    duration_seconds: float | None = None
    output: str = ""
    output_truncated: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "status": self.status.value,
            "exitCode": self.exit_code,
            "durationSeconds": self.duration_seconds,
            "output": self.output,
            "outputTruncated": self.output_truncated,
            "note": self.note,
        }


def not_run(commands: Iterable[TestCommand], status: TestStatus, note: str) -> tuple[TestRun, ...]:
    return tuple(TestRun(c.name, c.argv, status, note=note) for c in commands)


def secret_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Values of secret-looking variables in the caller's environment, for scrubbing."""
    return tuple(
        v for k, v in environ.items() if (k in _NEVER_PASS or _SECRET_NAME.search(k)) and len(v) >= 6
    )


def _scrub(text: str, secrets: Sequence[str]) -> str:
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    return text


def command_env(command: TestCommand, home: Path, environ: Mapping[str, str]) -> dict[str, str]:
    env = {k: environ[k] for k in (*_BASE_ENV, *command.pass_env) if k in environ}
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), TMPDIR=str(tmp), TERM="dumb")
    return env


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_test_command(
    command: TestCommand, cwd: Path, home: Path, *, environ: Mapping[str, str] | None = None
) -> TestRun:
    """Run one command once with shell=False and a hard timeout. Never retried."""
    environ = os.environ if environ is None else environ
    secrets = secret_values(environ)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(command.argv),
            cwd=cwd,
            env=command_env(command, home, environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,  # own process group, so a timeout kills its children too
        )
    except OSError as exc:
        return TestRun(command.name, command.argv, TestStatus.NOT_STARTED, note=f"could not start: {exc.strerror}")

    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=command.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        # The leader is not reaped yet, so its process-group ID cannot have been reused.
        _kill_group(proc.pid)
        try:
            raw, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a descendant that left the group still holds the pipe
            proc.stdout.close()
            proc.wait()
            raw = b""
    duration = round(time.monotonic() - started, 3)

    output = _scrub(raw.decode("utf-8", "replace"), secrets)
    truncated = len(output) > MAX_OUTPUT_CHARS
    if truncated:
        output = output[-MAX_OUTPUT_CHARS:]
    if timed_out:
        return TestRun(
            command.name, command.argv, TestStatus.TIMED_OUT, exit_code=proc.returncode,
            duration_seconds=duration, output=output, output_truncated=truncated,
            note=f"killed after {command.timeout_seconds:g}s; the outcome is incomplete and was not retried",
        )
    status = TestStatus.PASSED if proc.returncode == 0 else TestStatus.FAILED
    return TestRun(
        command.name, command.argv, status, exit_code=proc.returncode,
        duration_seconds=duration, output=output, output_truncated=truncated,
    )


def run_test_commands(
    commands: Sequence[TestCommand], cwd: Path, home: Path, *, environ: Mapping[str, str] | None = None
) -> tuple[TestRun, ...]:
    """Run every command in order, each exactly once, whatever the earlier results."""
    return tuple(run_test_command(c, cwd, home, environ=environ) for c in commands)


__all__ = [
    "DEFAULT_TEST_TIMEOUT_SECONDS",
    "TestCommand",
    "TestConfigError",
    "TestRun",
    "TestStatus",
    "load_test_config",
    "not_run",
    "parse_test_command",
    "run_test_command",
    "run_test_commands",
]
