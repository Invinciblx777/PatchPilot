"""Module 4: repository checks, patch validation, and isolated application.

Parser tests are pure unit tests on literal diff text (no git). Every other
test uses a real temporary Git repository and the real git binary
(tests/patch_helpers.py). No GitHub, AgntID, or network access is involved.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from patchpilot.patch_engine import Outcome, run_patch_workflow
from patchpilot.patch_verification import TestCommand, TestStatus
from patchpilot.patch_workspace import (
    GitError,
    PatchPolicy,
    PatchRejectedError,
    git,
    parse_patch,
)

from .patch_helpers import FIX, commit_all, make_patch, make_repo, patchpilot_branches, sh_git, worktree_count

POLICY = PatchPolicy()
APPROVE = lambda stage, detail: True  # noqa: E731


def _diff(path: str, old: str = "a\n", new: str = "b\n") -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1 +1 @@\n-{old}+{new}"
    )


# --------------------------------------------------------------------------- #
# Patch parsing (unit tests on literal diff text)
# --------------------------------------------------------------------------- #


def test_valid_diff_is_summarized(tmp_path):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, {**FIX, "docs/new.md": "hello\n", "README.md": None})
    proposal = parse_patch(patch, POLICY)
    assert {f.kind: f.new_path or f.old_path for f in proposal.files} == {
        "modify": "calc.py", "create": "docs/new.md", "delete": "README.md"
    }
    assert "modify  calc.py (+1 -1)" in proposal.summary()


@pytest.mark.parametrize("patch", ["", "   \n\n", "hello world\n", "--- a/x\n+++ b/x\n"])
def test_empty_or_changeless_patch_is_invalid(patch):
    with pytest.raises(PatchRejectedError) as info:
        parse_patch(patch, POLICY)
    assert info.value.kind == "invalid"


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-a\n+b\n", "ends inside a hunk"),
        ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n+c\n", "unrecognized line"),
        ("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n", "hunk without"),
        ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n*b\n", "hunk ends early"),
        ("diff --git a/x b/x\n--- x\n+++ x\n@@ -1 +1 @@\n-a\n+b\n", "a/"),
        ("diff --git a/x b/y\n--- a/x\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n", "without a rename"),
        ("diff --git a/x b/x\n--- a/z\n+++ b/z\n@@ -1 +1 @@\n-a\n+b\n", "disagree"),
    ],
)
def test_malformed_patches_are_rejected_not_repaired(patch, expected):
    with pytest.raises(PatchRejectedError) as info:
        parse_patch(patch, POLICY)
    assert info.value.kind == "invalid"
    assert any(expected in p for p in info.value.problems), info.value.problems


def test_non_utf8_and_nul_bytes_are_rejected():
    with pytest.raises(PatchRejectedError, match="UTF-8"):
        parse_patch(_diff("x").encode() + b"\xff\n", POLICY)
    with pytest.raises(PatchRejectedError, match="NUL"):
        parse_patch(_diff("x").encode() + b"\0", POLICY)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside.txt", "src/../../outside.txt", "./x", "a//b", "C:/x", "back\\slash"],
)
def test_absolute_and_traversal_paths_are_unsafe(path):
    with pytest.raises(PatchRejectedError) as info:
        parse_patch(_diff(path), POLICY)
    assert info.value.kind == "unsafe"


@pytest.mark.parametrize(
    "path", [".git/config", "sub/.git/hooks/pre-commit", ".GIT/config", ".git./x", ".gitmodules", "a/.gitattributes"]
)
def test_git_metadata_and_control_files_are_unsafe_even_if_allowed(path):
    policy = PatchPolicy(allowed_sensitive_patterns=("*", ".git/*", ".gitmodules", ".gitattributes"))
    with pytest.raises(PatchRejectedError) as info:
        parse_patch(_diff(path), policy)
    assert info.value.kind == "unsafe"


@pytest.mark.parametrize(
    "path",
    [".env", "config/.env.production", "deploy/server.pem", "keys/id_ed25519", ".github/workflows/ci.yml",
     ".npmrc", "infra/prod.tfvars", "home/.netrc"],
)
def test_sensitive_paths_are_denied_by_default(path):
    with pytest.raises(PatchRejectedError) as info:
        parse_patch(_diff(path), POLICY)
    assert info.value.kind == "unsafe"
    assert "sensitive pattern" in info.value.problems[0]


def test_sensitive_path_allowed_only_by_explicit_trusted_policy():
    patch = _diff(".env.example")
    with pytest.raises(PatchRejectedError):
        parse_patch(patch, POLICY)
    assert parse_patch(patch, PatchPolicy(allowed_sensitive_patterns=(".env.example",))).paths == (".env.example",)


def test_policy_text_inside_a_patch_has_no_effect():
    patch = "allow-sensitive-path: .env\nPATCHPILOT_ALLOW=*\n" + _diff(".env")
    with pytest.raises(PatchRejectedError, match="sensitive pattern"):
        parse_patch(patch, POLICY)


def test_symlinks_submodules_and_binary_changes_are_rejected(tmp_path):
    symlink = "diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+/etc\n"
    with pytest.raises(PatchRejectedError, match="symbolic link"):
        parse_patch(symlink, POLICY)
    gitlink = "diff --git a/m b/m\nnew file mode 160000\n--- /dev/null\n+++ b/m\n@@ -0,0 +1 @@\n+Subproject commit 0\n"
    with pytest.raises(PatchRejectedError, match="submodule"):
        parse_patch(gitlink, POLICY)
    binary = "diff --git a/b.bin b/b.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\nliteral 2\n"
    with pytest.raises(PatchRejectedError, match="binary"):
        parse_patch(binary, POLICY)


# --------------------------------------------------------------------------- #
# Repository validation (real git)
# --------------------------------------------------------------------------- #


def test_non_git_and_missing_paths_are_invalid_repositories(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for path in (plain, tmp_path / "missing"):
        result = run_patch_workflow(path, "HEAD", _diff("x"), approve=APPROVE)
        assert result.outcome is Outcome.INVALID_REPOSITORY
        assert result.workspace_path is None


def test_subdirectory_and_bare_repositories_are_rejected(tmp_path):
    repo = make_repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    sub = run_patch_workflow(repo / "pkg", "HEAD", _diff("x"), approve=APPROVE)
    assert sub.outcome is Outcome.INVALID_REPOSITORY and "pass the repository root" in sub.problems[0]
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    assert run_patch_workflow(bare, "HEAD", _diff("x"), approve=APPROVE).outcome is Outcome.INVALID_REPOSITORY


@pytest.mark.parametrize("base", ["no-such-branch", "", "--output=PWNED", "-h"])
def test_unresolvable_or_option_like_base_is_rejected(tmp_path, base):
    repo = make_repo(tmp_path)
    result = run_patch_workflow(repo, base, make_patch(repo, FIX), approve=APPROVE)
    assert result.outcome is Outcome.INVALID_REPOSITORY
    assert not (repo / "PWNED").exists()


def test_uncommitted_changes_are_refused_and_never_touched(tmp_path):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, FIX)
    (repo / "calc.py").write_text("# my unsaved work\n")
    (repo / "notes.txt").write_text("untracked\n")
    result = run_patch_workflow(repo, "HEAD", patch, approve=APPROVE)
    assert result.outcome is Outcome.UNCOMMITTED_CHANGES
    assert worktree_count(repo) == 1
    assert (repo / "calc.py").read_text() == "# my unsaved work\n"

    allowed = run_patch_workflow(repo, "HEAD", patch, approve=APPROVE, allow_uncommitted=True)
    assert allowed.outcome is Outcome.PATCH_APPLIED  # applied to the committed base, in the worktree
    assert allowed.original_dirty and allowed.original_preserved
    assert (repo / "calc.py").read_text() == "# my unsaved work\n"
    assert (repo / "notes.txt").read_text() == "untracked\n"


# --------------------------------------------------------------------------- #
# Applying in isolation (real git)
# --------------------------------------------------------------------------- #


def test_valid_patch_applies_only_in_a_disposable_worktree(tmp_path):
    repo = make_repo(tmp_path)
    head = sh_git(repo, "rev-parse", "HEAD").strip()
    status_before = sh_git(repo, "status", "--porcelain")
    parent = tmp_path / "workspaces"
    parent.mkdir()
    result = run_patch_workflow(repo, "main", make_patch(repo, FIX), approve=APPROVE, workspace_parent=parent)

    assert result.outcome is Outcome.PATCH_APPLIED
    assert result.base_sha == head
    assert result.changed_paths == ("calc.py",)
    assert "-    return a - b\n+    return a + b" in result.diff
    assert Path(result.workspace_path).is_relative_to(parent.resolve())
    assert result.workspace_branch.startswith("patchpilot/workspace-")
    # Original repository: same HEAD, same branch, same files, same status.
    assert result.original_preserved is True
    assert sh_git(repo, "rev-parse", "HEAD").strip() == head
    assert sh_git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
    assert sh_git(repo, "status", "--porcelain") == status_before
    # Cleanup removed the worktree, the local branch, and the temp directory.
    assert result.cleanup.status == "removed"
    assert worktree_count(repo) == 1 and patchpilot_branches(repo) == []
    assert list(parent.iterdir()) == []
    assert len(sh_git(repo, "log", "--oneline").splitlines()) == 1  # no commit was made


def test_selected_base_revision_is_used(tmp_path):
    repo = make_repo(tmp_path)
    first = sh_git(repo, "rev-parse", "HEAD").strip()
    patch = make_patch(repo, FIX)
    (repo / "calc.py").write_text("def add(a, b):\n    return b - a\n")
    commit_all(repo, "second")
    assert run_patch_workflow(repo, "HEAD", patch, approve=APPROVE).outcome is Outcome.PATCH_CHECK_FAILED
    old = run_patch_workflow(repo, first, patch, approve=APPROVE)
    assert old.outcome is Outcome.PATCH_APPLIED and old.base_sha == first


def test_patch_that_does_not_apply_is_a_check_failure(tmp_path):
    repo = make_repo(tmp_path)
    patch = _diff("calc.py", old="this line is not in the file\n", new="x\n")
    result = run_patch_workflow(repo, "HEAD", patch, approve=APPROVE)
    assert result.outcome is Outcome.PATCH_CHECK_FAILED
    assert "patch does not apply" in result.problems[0]
    assert result.diff == "" and result.cleanup.status == "removed"


def test_patch_through_a_committed_symlink_cannot_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = make_repo(tmp_path)
    os.symlink(outside, repo / "linkdir")
    commit_all(repo, "add symlink")
    patch = "diff --git a/linkdir/evil.txt b/linkdir/evil.txt\nnew file mode 100644\n--- /dev/null\n+++ b/linkdir/evil.txt\n@@ -0,0 +1 @@\n+owned\n"
    result = run_patch_workflow(repo, "HEAD", patch, approve=APPROVE)
    assert result.outcome is Outcome.PATCH_CHECK_FAILED
    assert "beyond a symbolic link" in result.problems[0]
    assert list(outside.iterdir()) == []


def test_check_only_and_declined_approval_apply_nothing(tmp_path):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, FIX)
    seen = []
    check = run_patch_workflow(repo, "HEAD", patch, approve=lambda *a: seen.append(a) or True, check_only=True)
    assert check.outcome is Outcome.CHECK_PASSED and seen == [] and check.diff == ""
    declined = run_patch_workflow(repo, "HEAD", patch, approve=lambda stage, detail: seen.append(detail) or False)
    assert declined.outcome is Outcome.NOT_APPLIED
    assert seen == ["modify  calc.py (+1 -1)"]  # the operator saw the summary before deciding


def test_keep_workspace_leaves_it_for_inspection(tmp_path):
    repo = make_repo(tmp_path)
    result = run_patch_workflow(repo, "HEAD", make_patch(repo, FIX), approve=APPROVE, keep_workspace=True)
    try:
        assert result.cleanup.status == "kept"
        assert (Path(result.workspace_path) / "calc.py").read_text().endswith("return a + b\n")
        assert patchpilot_branches(repo) == [result.workspace_branch]
        assert (repo / "calc.py").read_text().endswith("return a - b\n")
    finally:
        sh_git(repo, "worktree", "remove", "--force", result.workspace_path)
        sh_git(repo, "branch", "-D", result.workspace_branch)
        Path(result.workspace_path).parent.rmdir()  # the empty temporary parent


def test_workspace_is_cleaned_up_when_the_workflow_raises(tmp_path):
    repo = make_repo(tmp_path)
    parent = tmp_path / "workspaces"
    parent.mkdir()

    def explode(stage, detail):
        raise RuntimeError("operator interface crashed")

    with pytest.raises(RuntimeError, match="crashed"):
        run_patch_workflow(repo, "HEAD", make_patch(repo, FIX), approve=explode, workspace_parent=parent)
    assert list(parent.iterdir()) == []
    assert worktree_count(repo) == 1 and patchpilot_branches(repo) == []


def test_workspace_inside_the_repository_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "ws").mkdir()
    result = run_patch_workflow(repo, "HEAD", make_patch(repo, FIX), approve=APPROVE, workspace_parent=repo / "ws")
    assert result.outcome is Outcome.WORKSPACE_ERROR
    assert "outside the original repository" in result.problems[0]


# --------------------------------------------------------------------------- #
# Tests in the workflow (real git, real subprocesses)
# --------------------------------------------------------------------------- #

PYTEST = TestCommand("unit", (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"), timeout_seconds=120)


def test_applied_patch_with_passing_and_failing_tests(tmp_path):
    repo = make_repo(tmp_path)
    good = run_patch_workflow(repo, "HEAD", make_patch(repo, FIX), approve=APPROVE, tests=[PYTEST])
    assert good.outcome is Outcome.TESTS_PASSED
    assert good.tests[0].status is TestStatus.PASSED and good.tests[0].exit_code == 0

    bad = run_patch_workflow(
        repo, "HEAD", make_patch(repo, {"calc.py": "def add(a, b):\n    return a * b\n"}), approve=APPROVE, tests=[PYTEST]
    )
    assert bad.outcome is Outcome.TESTS_FAILED and bad.applied
    assert bad.tests[0].exit_code == 1
    assert "assert 6 == 5" in bad.tests[0].output


def test_rejected_patch_runs_no_tests(tmp_path):
    repo = make_repo(tmp_path)
    marker = tmp_path / "ran"
    probe = TestCommand("probe", (sys.executable, "-c", f"open({str(marker)!r}, 'w')"))
    result = run_patch_workflow(repo, "HEAD", _diff(".env"), approve=APPROVE, tests=[probe])
    assert result.outcome is Outcome.UNSAFE_PATCH
    assert result.tests[0].status is TestStatus.NOT_RUN
    assert not marker.exists()


def test_declining_tests_marks_them_skipped(tmp_path):
    repo = make_repo(tmp_path)
    result = run_patch_workflow(
        repo, "HEAD", make_patch(repo, FIX), approve=lambda stage, detail: stage == "apply", tests=[PYTEST]
    )
    assert result.outcome is Outcome.PATCH_APPLIED
    assert result.tests[0].status is TestStatus.SKIPPED


# --------------------------------------------------------------------------- #
# No remote operations, no execution of untrusted text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sub", ["push", "fetch", "pull", "commit", "merge", "reset", "clean", "checkout", "remote", "clone"])
def test_git_helper_refuses_history_and_remote_subcommands(tmp_path, sub):
    repo = make_repo(tmp_path)
    with pytest.raises(GitError, match="not permitted"):
        git(repo, sub)


def test_workflow_never_touches_a_configured_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = make_repo(tmp_path)
    sh_git(repo, "remote", "add", "origin", str(remote))
    sh_git(repo, "push", "-q", "origin", "main")  # test setup only
    refs_before = sh_git(remote, "show-ref")
    result = run_patch_workflow(repo, "HEAD", make_patch(repo, FIX), approve=APPROVE, tests=[PYTEST])
    assert result.outcome is Outcome.TESTS_PASSED
    assert sh_git(remote, "show-ref") == refs_before
    assert "patchpilot" not in sh_git(repo, "branch", "-a")


def test_shell_syntax_in_patch_paths_and_content_is_never_executed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path)
    patch = make_patch(
        repo,
        {
            "$(touch PWNED1).txt": "`touch PWNED2`\n$(touch PWNED3)\n",
            ".patchpilot/tests.json": '{"commands": [{"name": "x", "argv": ["touch", "PWNED4"]}]}\n',
        },
    )
    result = run_patch_workflow(repo, "HEAD", patch, approve=APPROVE)
    assert result.outcome is Outcome.PATCH_APPLIED
    assert result.tests == ()  # nothing in the repository or patch can add a test command
    assert "$(touch PWNED1).txt" in result.changed_paths
    for root in (tmp_path, repo, Path(os.getcwd())):
        assert not list(root.rglob("PWNED*")), "untrusted text was executed"


def test_apply_is_atomic_and_never_partial(tmp_path):
    # Calls the apply step directly, without the preceding check, to prove a
    # half-applicable patch changes nothing (no --reject / partial application).
    from patchpilot.patch_workspace import Workspace, apply_patch, open_repository

    repo = make_repo(tmp_path)
    good = make_patch(repo, FIX)
    bad = _diff("README.md", old="not in the file\n", new="x\n")
    proposal = parse_patch(good + bad, POLICY)
    opened = open_repository(repo)
    ws = Workspace.create(opened, sh_git(repo, "rev-parse", "HEAD").strip())
    try:
        with pytest.raises(GitError, match="patch does not apply"):
            apply_patch(ws, proposal)
        assert (ws.path / "calc.py").read_text().endswith("return a - b\n")
        assert sh_git(ws.path, "status", "--porcelain") == ""
        assert not list(ws.path.rglob("*.rej"))
    finally:
        assert ws.cleanup().status == "removed"
