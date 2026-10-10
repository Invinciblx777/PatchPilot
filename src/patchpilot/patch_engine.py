"""Patch workflow: validate, check, apply in a disposable worktree, test, clean up.

The patch is an INPUT. This module does not generate fixes, and a passing test
run does not show that a GitHub issue is resolved: it shows only that the
operator's configured commands exited 0 against the patched workspace.

Nothing here contacts GitHub or AgntID. Local path rules are PatchPilot
safeguards, not AgntID enforcement.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from patchpilot.patch_verification import TestCommand, TestRun, TestStatus, not_run, run_test_commands
from patchpilot.patch_workspace import (
    CleanupReport,
    GitError,
    InvalidRepositoryError,
    PatchPolicy,
    PatchRejectedError,
    Workspace,
    WorkspaceError,
    apply_patch,
    check_patch,
    open_repository,
    parse_patch,
    resolve_base,
    snapshot_repository,
    unstaged_changes,
)

# approve(stage, detail) -> bool. stage is "apply" or "tests"; detail is what will happen.
Approver = Callable[[str, str], bool]


class Outcome(str, Enum):
    INVALID_REPOSITORY = "invalid_repository"
    UNCOMMITTED_CHANGES = "uncommitted_changes"  # refused: original tree has local changes
    INVALID_PATCH = "invalid_patch"  # empty, malformed, or unparseable
    UNSAFE_PATCH = "unsafe_patch"  # violates a path or content safeguard
    PATCH_CHECK_FAILED = "patch_check_failed"  # well-formed, but does not apply to the base
    CHECK_PASSED = "check_passed"  # --check-only: it would apply; nothing was applied
    NOT_APPLIED = "not_applied"  # the operator declined to apply it
    PATCH_APPLIED = "patch_applied"  # applied; no tests were run
    TESTS_PASSED = "tests_passed"  # applied; every configured command exited 0
    TESTS_FAILED = "tests_failed"  # applied; at least one command failed
    TESTS_INCOMPLETE = "tests_incomplete"  # applied; a command timed out or could not start
    WORKSPACE_ERROR = "workspace_error"


NOTES = (
    "Path checks are PatchPilot application safeguards, not AgntID enforcement.",
    "Passing tests show only that the configured commands exited 0 against this patch; "
    "they do not show that any GitHub issue is resolved.",
    "Test commands ran repository code with the operator's permissions; this is not an OS sandbox.",
)


@dataclass
class WorkflowResult:
    outcome: Outcome
    repository: str | None = None
    base_revision: str = ""
    base_sha: str | None = None
    original_head: str | None = None
    original_branch: str | None = None
    original_dirty: bool | None = None
    workspace_path: str | None = None
    workspace_branch: str | None = None
    files: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    diff: str = ""
    diff_stat: str = ""
    tests: tuple[TestRun, ...] = ()
    changed_by_tests: tuple[str, ...] = ()
    cleanup: CleanupReport | None = None
    original_preserved: bool | None = None
    notes: tuple[str, ...] = field(default=NOTES)

    @property
    def applied(self) -> bool:
        return self.outcome in (
            Outcome.PATCH_APPLIED, Outcome.TESTS_PASSED, Outcome.TESTS_FAILED, Outcome.TESTS_INCOMPLETE
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "patchApplied": self.applied,
            "repository": self.repository,
            "baseRevision": self.base_revision,
            "baseSha": self.base_sha,
            "originalHead": self.original_head,
            "originalBranch": self.original_branch,
            "originalDirty": self.original_dirty,
            "originalPreserved": self.original_preserved,
            "workspacePath": self.workspace_path,
            "workspaceBranch": self.workspace_branch,
            "files": list(self.files),
            "changedPaths": list(self.changed_paths),
            "problems": list(self.problems),
            "diffStat": self.diff_stat,
            "diff": self.diff,
            "tests": [t.to_dict() for t in self.tests],
            "changedByTests": list(self.changed_by_tests),
            "cleanup": None if self.cleanup is None else {
                "status": self.cleanup.status, "errors": list(self.cleanup.errors)
            },
            "notes": list(self.notes),
        }


def _tests_outcome(runs: Sequence[TestRun]) -> Outcome:
    statuses = {r.status for r in runs}
    if TestStatus.FAILED in statuses:
        return Outcome.TESTS_FAILED
    if statuses & {TestStatus.TIMED_OUT, TestStatus.NOT_STARTED}:
        return Outcome.TESTS_INCOMPLETE
    return Outcome.TESTS_PASSED


def run_patch_workflow(
    repository: str | os.PathLike[str],
    base_revision: str,
    patch: bytes | str,
    *,
    approve: Approver,
    policy: PatchPolicy = PatchPolicy(),
    tests: Sequence[TestCommand] = (),
    allow_uncommitted: bool = False,
    keep_workspace: bool = False,
    workspace_parent: Path | None = None,
    check_only: bool = False,
) -> WorkflowResult:
    """Run the local patch workflow once. Never commits, pushes, or retries."""
    result = WorkflowResult(Outcome.INVALID_REPOSITORY, base_revision=base_revision)
    try:
        repo = open_repository(repository)
        result.repository = str(repo.root)
        snap = repo.snapshot
        result.original_head, result.original_branch, result.original_dirty = snap.head, snap.branch, snap.dirty
        result.base_sha = resolve_base(repo, base_revision)
    except InvalidRepositoryError as exc:
        result.problems = (exc.message,)
        return result
    if snap.dirty and not allow_uncommitted:
        result.outcome = Outcome.UNCOMMITTED_CHANGES
        result.problems = ("the original working tree has uncommitted or untracked changes; commit or "
                           "stash them, or pass allow_uncommitted to patch the committed base anyway",)
        return result

    try:
        proposal = parse_patch(patch, policy)
    except PatchRejectedError as exc:
        result.outcome = Outcome.UNSAFE_PATCH if exc.kind == "unsafe" else Outcome.INVALID_PATCH
        result.problems = exc.problems
        result.tests = not_run(tests, TestStatus.NOT_RUN, "the patch was rejected")
        return result
    result.files = tuple(f.describe() for f in proposal.files)

    try:
        ws = Workspace.create(repo, result.base_sha, parent_dir=workspace_parent)
    except WorkspaceError as exc:
        result.outcome = Outcome.WORKSPACE_ERROR
        result.problems = (exc.message,)
        return result
    result.workspace_path, result.workspace_branch = str(ws.path), ws.branch

    try:
        _in_workspace(result, ws, proposal, tests, approve, check_only)
    finally:
        result.cleanup = ws.cleanup(keep=keep_workspace)
        result.original_preserved = snapshot_repository(repo.root) == snap
    return result


def _in_workspace(result, ws, proposal, tests, approve, check_only) -> None:
    check = check_patch(ws, proposal)
    if not check.ok:
        result.outcome = Outcome.INVALID_PATCH if check.kind == "invalid" else Outcome.PATCH_CHECK_FAILED
        result.problems = (check.message,)
        result.tests = not_run(tests, TestStatus.NOT_RUN, "the patch did not apply")
        return
    if check_only:
        result.outcome = Outcome.CHECK_PASSED
        result.tests = not_run(tests, TestStatus.NOT_RUN, "check only")
        return
    if not approve("apply", proposal.summary()):
        result.outcome = Outcome.NOT_APPLIED
        result.tests = not_run(tests, TestStatus.NOT_RUN, "the patch was not applied")
        return
    try:
        applied = apply_patch(ws, proposal)
    except (WorkspaceError, GitError) as exc:
        result.outcome = Outcome.WORKSPACE_ERROR
        result.problems = (exc.message,)
        result.tests = not_run(tests, TestStatus.NOT_RUN, "the patch did not apply")
        return
    result.diff, result.diff_stat, result.changed_paths = applied.diff, applied.diff_stat, applied.changed_paths
    result.outcome = Outcome.PATCH_APPLIED
    if not tests:
        return
    if not approve("tests", "\n".join(f"{t.name}: {t.display()} (timeout {t.timeout_seconds:g}s)" for t in tests)):
        result.tests = not_run(tests, TestStatus.SKIPPED, "the operator declined to run tests")
        return
    result.tests = run_test_commands(tests, ws.path, ws.temp_dir / "home")
    result.changed_by_tests = tuple(unstaged_changes(ws))
    result.outcome = _tests_outcome(result.tests)


__all__ = ["NOTES", "Approver", "Outcome", "WorkflowResult", "run_patch_workflow"]
