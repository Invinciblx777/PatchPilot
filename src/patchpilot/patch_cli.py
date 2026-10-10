"""Apply a supplied unified diff to a disposable worktree of a local repo and test it.

    patchpilot-patch --repo PATH --base REV --patch FILE --check-only
    patchpilot-patch --repo PATH --base REV --patch FILE --test-command "python -m pytest -q"
    patchpilot-patch --repo PATH --base REV --patch FILE --test-config tests.json --yes --json

Without --yes, the patch summary and each test command are shown and must be
approved on the terminal. Test commands come only from --test-command or
--test-config; nothing in the repository or the patch can add one.

Local only: no commit, push, branch publication, or pull request.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import TextIO

from patchpilot.discover_cli import _indent, _safe_text
from patchpilot.patch_engine import Outcome, WorkflowResult, run_patch_workflow
from patchpilot.patch_verification import (
    DEFAULT_TEST_TIMEOUT_SECONDS,
    TestConfigError,
    load_test_config,
    parse_test_command,
)
from patchpilot.patch_workspace import DEFAULT_SENSITIVE_PATTERNS, MAX_PATCH_BYTES, PatchPolicy

EXIT_CODES = {
    Outcome.TESTS_PASSED: 0,
    Outcome.CHECK_PASSED: 0,
    Outcome.INVALID_REPOSITORY: 10,
    Outcome.UNCOMMITTED_CHANGES: 10,
    Outcome.INVALID_PATCH: 11,
    Outcome.UNSAFE_PATCH: 12,
    Outcome.PATCH_CHECK_FAILED: 13,
    Outcome.NOT_APPLIED: 14,
    Outcome.PATCH_APPLIED: 15,
    Outcome.TESTS_FAILED: 16,
    Outcome.TESTS_INCOMPLETE: 17,
    Outcome.WORKSPACE_ERROR: 18,
}
EXIT_CLEANUP_FAILED = 19

_HEADLINES = {
    Outcome.TESTS_PASSED: "patch applied; all configured test commands passed",
    Outcome.CHECK_PASSED: "patch would apply cleanly; nothing was applied (--check-only)",
    Outcome.INVALID_REPOSITORY: "INVALID REPOSITORY; nothing was done",
    Outcome.UNCOMMITTED_CHANGES: "REFUSED: the repository has uncommitted changes; nothing was done",
    Outcome.INVALID_PATCH: "INVALID PATCH; nothing was applied",
    Outcome.UNSAFE_PATCH: "UNSAFE PATCH rejected by PatchPilot's path safeguards; nothing was applied",
    Outcome.PATCH_CHECK_FAILED: "PATCH DOES NOT APPLY to the base revision; nothing was applied",
    Outcome.NOT_APPLIED: "not applied (declined)",
    Outcome.PATCH_APPLIED: "patch applied; NO tests were run",
    Outcome.TESTS_FAILED: "patch applied; TESTS FAILED (this is not a successful fix)",
    Outcome.TESTS_INCOMPLETE: "patch applied; TESTS INCOMPLETE (timed out or could not start)",
    Outcome.WORKSPACE_ERROR: "WORKSPACE ERROR",
}


def _prompt_approver(stdin: TextIO, stdout: TextIO):
    def approve(stage: str, detail: str) -> bool:
        question = (
            "Apply this patch in the isolated workspace?"
            if stage == "apply"
            else "Run these test commands? They execute repository code with your permissions."
        )
        stdout.write(f"\n{_safe_text(detail)}\n{question} [y/N] ")
        stdout.flush()
        answer = stdin.readline()
        return answer.strip().lower() in ("y", "yes")

    return approve


def format_result(result: WorkflowResult) -> str:
    out = [
        "PatchPilot local patch workspace",
        f"  outcome:        {result.outcome.value}: {_HEADLINES[result.outcome]}",
        f"  repository:     {result.repository or '(invalid)'}",
        f"  base:           {result.base_revision} -> {result.base_sha or '(unresolved)'}",
    ]
    if result.original_head is not None:
        out.append(f"  original HEAD:  {result.original_head} ({result.original_branch or 'detached'})"
                   f"{', has local changes' if result.original_dirty else ''}")
    if result.workspace_path:
        out.append(f"  workspace:      {result.workspace_path}")
        out.append(f"  local branch:   {result.workspace_branch}")
    for problem in result.problems:
        out.append(f"  problem:        {_safe_text(problem)}")
    if result.files:
        out.append("  patch files:")
        out += [f"    {_safe_text(f)}" for f in result.files]
    if result.diff:
        out += ["", "Resulting diff (git diff --cached in the workspace):", _safe_text(result.diff.rstrip())]
    if result.tests:
        out += ["", "Tests (operator-configured commands only)"]
        for run in result.tests:
            code = "" if run.exit_code is None else f", exit code {run.exit_code}"
            took = "" if run.duration_seconds is None else f", {run.duration_seconds:g}s"
            out.append(f"  {run.name}: {run.status.value.upper()}{code}{took}")
            out.append(f"    argv: {_safe_text(json.dumps(list(run.argv)))}")
            if run.note:
                out.append(f"    note: {_safe_text(run.note)}")
            if run.output and run.status.value != "passed":
                label = "output (last part)" if run.output_truncated else "output"
                out.append(f"    {label}:")
                out.append(_indent(_safe_text(run.output.rstrip()), "      "))
    if result.changed_by_tests:
        out.append(f"  files changed by test commands: {', '.join(_safe_text(p) for p in result.changed_by_tests)}")
    if result.cleanup is not None:
        out.append(f"  cleanup:        {result.cleanup.status}")
        out += [f"    error: {_safe_text(e)}" for e in result.cleanup.errors]
        if result.cleanup.status == "kept":
            repo, path = shlex.quote(result.repository), shlex.quote(result.workspace_path)
            out.append("    remove later with:")
            out.append(f"      git -C {repo} worktree remove --force {path}")
            out.append(f"      git -C {repo} branch -D {shlex.quote(result.workspace_branch)}")
            out.append(f"      rmdir {shlex.quote(str(Path(result.workspace_path).parent))}")
    if result.original_preserved is not None:
        out.append(f"  original tree:  {'unchanged' if result.original_preserved else 'CHANGED (unexpected)'}")
    out += ["", *(f"  note: {n}" for n in result.notes)]
    return "\n".join(out) + "\n"


def exit_code(result: WorkflowResult) -> int:
    if result.cleanup is not None and result.cleanup.status == "failed":
        return EXIT_CLEANUP_FAILED
    return EXIT_CODES[result.outcome]


def run(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="patchpilot-patch",
        description="Check, apply, and test a unified diff in a disposable worktree of a local Git repository.",
    )
    parser.add_argument("--repo", required=True, type=Path, help="root of the local Git working tree")
    parser.add_argument("--base", required=True, help="base revision (commit, branch, or tag)")
    parser.add_argument("--patch", required=True, type=Path, help="unified diff file (git diff format, -p1)")
    parser.add_argument("--check-only", action="store_true", help="validate and check; do not apply")
    parser.add_argument("--test-command", action="append", default=[], metavar="CMD",
                        help="test command, split with shell rules but never run by a shell (repeatable)")
    parser.add_argument("--test-config", type=Path, help='JSON file: {"commands": [{"name", "argv", ...}]}')
    parser.add_argument("--test-timeout", type=float, default=DEFAULT_TEST_TIMEOUT_SECONDS,
                        help="timeout in seconds for each --test-command (default: 600)")
    parser.add_argument("--yes", action="store_true", help="non-interactive: approve applying and testing")
    parser.add_argument("--keep-workspace", action="store_true", help="keep the worktree and local branch")
    parser.add_argument("--allow-uncommitted", action="store_true",
                        help="proceed although the original tree has local changes (it is still not touched)")
    parser.add_argument("--allow-sensitive-path", action="append", default=[], metavar="PATTERN",
                        help="explicitly allow paths matching PATTERN despite the sensitive-path default deny")
    parser.add_argument("--workspace-dir", type=Path, help="parent directory for the workspace (default: system temp)")
    parser.add_argument("--json", action="store_true", help="print a JSON result")
    args = parser.parse_args(argv)
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr

    try:
        if args.patch.stat().st_size > MAX_PATCH_BYTES:
            raise TestConfigError(f"{args.patch} is larger than {MAX_PATCH_BYTES} bytes")
        patch = args.patch.read_bytes()
        tests = list(load_test_config(args.test_config)) if args.test_config else []
        tests += [
            parse_test_command(cmd, timeout_seconds=args.test_timeout, index=i)
            for i, cmd in enumerate(args.test_command, start=len(tests) + 1)
        ]
    except OSError as exc:
        stderr.write(f"patchpilot-patch: cannot read {exc.filename}: {exc.strerror}\n")
        return 2
    except TestConfigError as exc:
        stderr.write(f"patchpilot-patch: {exc.message}\n")
        return 2

    policy = PatchPolicy(DEFAULT_SENSITIVE_PATTERNS, tuple(args.allow_sensitive_path))
    approve = (lambda stage, detail: True) if args.yes else _prompt_approver(stdin, stdout)
    result = run_patch_workflow(
        args.repo,
        args.base,
        patch,
        approve=approve,
        policy=policy,
        tests=tests,
        allow_uncommitted=args.allow_uncommitted,
        keep_workspace=args.keep_workspace,
        workspace_parent=args.workspace_dir,
        check_only=args.check_only,
    )
    code = exit_code(result)
    if args.json:
        stdout.write(json.dumps({"ok": code == 0, "exitCode": code, **result.to_dict()}, indent=2) + "\n")
    else:
        stdout.write(format_result(result))
    return code


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
