"""Module 4: test-command execution. Real subprocesses (sys.executable), no mocks."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from patchpilot.patch_verification import (
    MAX_OUTPUT_CHARS,
    TestCommand,
    TestConfigError,
    TestStatus,
    load_test_config,
    parse_test_command,
    run_test_command,
)


def _py(code: str, *, name="probe", timeout=30.0, pass_env=()) -> TestCommand:
    return TestCommand(name, (sys.executable, "-c", code), timeout_seconds=timeout, pass_env=pass_env)


@pytest.fixture
def dirs(tmp_path):
    work, home = tmp_path / "work", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    return work, home


def test_passing_and_failing_commands_report_real_exit_codes_and_output(dirs):
    work, home = dirs
    ok = run_test_command(_py("print('all good')"), work, home)
    assert (ok.status, ok.exit_code) == (TestStatus.PASSED, 0)
    assert ok.output.strip() == "all good"
    bad = run_test_command(_py("import sys; print('boom', file=sys.stderr); sys.exit(3)"), work, home)
    assert (bad.status, bad.exit_code) == (TestStatus.FAILED, 3)
    assert "boom" in bad.output  # stderr is captured too


def test_command_runs_in_the_workspace(dirs):
    work, home = dirs
    run = run_test_command(_py("import os; print(os.getcwd())"), work, home)
    assert run.output.strip() == str(work)


def test_timeout_is_reported_incomplete_kills_children_and_is_not_retried(dirs, tmp_path):
    work, home = dirs
    starts, child_pid = tmp_path / "starts", tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        f"open({str(starts)!r}, 'a').write('start\\n')\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(child_pid)!r}, 'w').write(str(child.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(60)\n"
    )
    began = time.monotonic()
    run = run_test_command(_py(code, timeout=1.0), work, home)
    assert time.monotonic() - began < 15
    assert run.status is TestStatus.TIMED_OUT
    assert "started" in run.output and "incomplete" in run.note and "not retried" in run.note
    assert starts.read_text() == "start\n"  # exactly one attempt
    pid = int(child_pid.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):  # the child in the same process group was killed
        os.kill(pid, 0)


def test_missing_program_is_not_started(dirs):
    work, home = dirs
    run = run_test_command(TestCommand("missing", ("/nonexistent/patchpilot-tool",)), work, home)
    assert run.status is TestStatus.NOT_STARTED and run.exit_code is None


def test_environment_is_minimal_and_secrets_are_scrubbed(dirs):
    work, home = dirs
    environ = {
        "PATH": os.environ["PATH"],
        "AGNTID_ACCESS_TOKEN": "agntid-secret-value-1",
        "GITHUB_TOKEN": "ghp_secret-value-2",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value-3",
        "HOME": "/home/real-user",
        "DEPLOY_PASSWORD": "deploy-secret-value-4",
    }
    dump = "import json, os; print(json.dumps(dict(os.environ)))"
    run = run_test_command(_py(dump), work, home, environ=environ)
    env = json.loads(run.output)
    # Python itself may add LC_CTYPE under a C locale (PEP 538); nothing else is added.
    assert set(env) - {"LC_CTYPE"} == {"PATH", "HOME", "TMPDIR", "TERM"}
    assert env["HOME"] == str(home) and env["PATH"] == environ["PATH"]
    assert "AGNTID_ACCESS_TOKEN" not in env and "GITHUB_TOKEN" not in env

    # A variable passed on purpose still never appears in the reported output.
    leak = run_test_command(
        _py("import os; print(os.environ['DEPLOY_PASSWORD'])", pass_env=("DEPLOY_PASSWORD",)), work, home, environ=environ
    )
    assert "deploy-secret-value-4" not in leak.output and "[REDACTED]" in leak.output


def test_runtime_and_github_tokens_cannot_be_passed():
    for name in ("AGNTID_ACCESS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        with pytest.raises(TestConfigError, match="may not receive"):
            TestCommand("x", ("true",), pass_env=(name,))


def test_long_output_keeps_the_end(dirs):
    work, home = dirs
    run = run_test_command(_py(f"print('x' * {MAX_OUTPUT_CHARS * 3}); print('THE END')"), work, home)
    assert run.output_truncated and len(run.output) == MAX_OUTPUT_CHARS
    assert run.output.rstrip().endswith("THE END")


def test_shell_metacharacters_are_plain_arguments(dirs, tmp_path):
    work, home = dirs
    command = parse_test_command(
        f"'{sys.executable}' -c 'import sys; print(sys.argv[1:])' ';' touch PWNED '&&' 'rm -rf x' '$(id)'"
    )
    run = run_test_command(command, work, home)
    assert run.status is TestStatus.PASSED
    assert "';', 'touch', 'PWNED', '&&', 'rm -rf x', '$(id)'" in run.output
    assert not (work / "PWNED").exists() and not (tmp_path / "PWNED").exists()


def test_test_config_file_is_strict(tmp_path):
    good = tmp_path / "tests.json"
    good.write_text(json.dumps({"commands": [{"name": "unit", "argv": ["pytest", "-q"], "timeout_seconds": 30}]}))
    [command] = load_test_config(good)
    assert (command.name, command.argv, command.timeout_seconds) == ("unit", ("pytest", "-q"), 30)
    for bad in (
        {"commands": [{"name": "unit", "argv": "pytest -q"}]},  # a string would need a shell
        {"commands": [{"name": "unit", "argv": ["pytest"], "shell": True}]},
        {"commands": [{"name": "u", "argv": ["a"]}, {"name": "u", "argv": ["b"]}]},
        {"commands": [{"name": "u", "argv": ["a"], "timeout_seconds": 0}]},
        {"tests": []},
    ):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(TestConfigError):
            load_test_config(path)
