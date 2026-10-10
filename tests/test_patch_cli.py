"""patchpilot-patch end to end on real temporary Git repositories (no network)."""

from __future__ import annotations

import io
import json
import shlex
import sys

from patchpilot.patch_cli import run

from .patch_helpers import FIX, WRONG_FIX, make_patch, make_repo, patchpilot_branches, worktree_count

PYTEST = shlex.join([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])


def _run(argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    code = run(argv, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _setup(tmp_path, edits=FIX):
    repo = make_repo(tmp_path)
    patch = tmp_path / "change.patch"
    patch.write_text(make_patch(repo, edits))
    return repo, patch


def test_noninteractive_run_reports_diff_tests_workspace_and_cleanup(tmp_path):
    repo, patch = _setup(tmp_path)
    code, out, err = _run(["--repo", str(repo), "--base", "main", "--patch", str(patch),
                           "--test-command", PYTEST, "--yes", "--json"])
    assert code == 0, err
    report = json.loads(out)
    assert report["outcome"] == "tests_passed" and report["patchApplied"] is True
    assert report["baseSha"] == report["originalHead"]
    assert report["changedPaths"] == ["calc.py"]
    assert "+    return a + b" in report["diff"]
    assert report["tests"][0]["status"] == "passed" and report["tests"][0]["exitCode"] == 0
    assert report["cleanup"] == {"status": "removed", "errors": []}
    assert report["originalPreserved"] is True
    assert any("do not show that any GitHub issue is resolved" in n for n in report["notes"])
    assert worktree_count(repo) == 1 and patchpilot_branches(repo) == []


def test_failing_tests_are_not_reported_as_a_fix(tmp_path):
    repo, patch = _setup(tmp_path, WRONG_FIX)
    code, out, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--test-command", PYTEST, "--yes"])
    assert code == 16
    assert "tests_failed: patch applied; TESTS FAILED (this is not a successful fix)" in out
    assert "FAILED, exit code 1" in out and "assert 6 == 5" in out


def test_applied_without_tests_says_so(tmp_path):
    repo, patch = _setup(tmp_path)
    code, out, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--yes"])
    assert code == 15
    assert "patch applied; NO tests were run" in out


def test_interactive_approval_shows_summary_and_can_decline(tmp_path):
    repo, patch = _setup(tmp_path)
    code, out, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--test-command", PYTEST], "n\n")
    assert code == 14
    assert "modify  calc.py (+1 -1)\nApply this patch in the isolated workspace? [y/N]" in out
    assert "not applied (declined)" in out
    code, out, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--test-command", PYTEST], "y\nn\n")
    assert code == 15
    assert "Run these test commands? They execute repository code" in out
    assert "SKIPPED" in out


def test_no_answer_on_stdin_means_declined(tmp_path):
    repo, patch = _setup(tmp_path)
    code, _, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch)], "")
    assert code == 14


def test_check_only_applies_nothing(tmp_path):
    repo, patch = _setup(tmp_path)
    code, out, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--check-only"])
    assert code == 0
    assert "would apply cleanly; nothing was applied" in out
    assert "Resulting diff" not in out


def test_rejections_have_distinct_exit_codes(tmp_path):
    repo, patch = _setup(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run(["--repo", str(plain), "--base", "HEAD", "--patch", str(patch)])[0] == 10
    unsafe = tmp_path / "unsafe.patch"
    unsafe.write_text("diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-a\n+b\n")
    code, out, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(unsafe), "--yes"])
    assert code == 12 and "UNSAFE PATCH rejected by PatchPilot's path safeguards" in out
    stale = tmp_path / "stale.patch"
    stale.write_text("diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-nope\n+x\n")
    assert _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(stale), "--yes"])[0] == 13
    garbage = tmp_path / "garbage.patch"
    garbage.write_text("this is not a diff\n")
    assert _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(garbage), "--yes"])[0] == 11


def test_sensitive_path_needs_an_explicit_operator_flag(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x\n", ".env.example": "A=1\n"})
    patch = tmp_path / "env.patch"
    patch.write_text(make_patch(repo, {".env.example": "A=2\n"}))
    base = ["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--yes"]
    assert _run(base)[0] == 12
    assert _run([*base, "--allow-sensitive-path", ".env.example"])[0] == 15


def test_bad_inputs_are_usage_errors(tmp_path):
    repo, patch = _setup(tmp_path)
    assert _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(tmp_path / "missing.patch")])[0] == 2
    config = tmp_path / "tests.json"
    config.write_text('{"commands": [{"name": "x", "argv": "make test"}]}')
    code, _, err = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--test-config", str(config)])
    assert code == 2 and "must be lists" in err


def test_secrets_in_the_environment_never_reach_tests_or_output(tmp_path, monkeypatch):
    secret = "agntid-token-should-never-print-77"
    monkeypatch.setenv("AGNTID_ACCESS_TOKEN", secret)
    monkeypatch.setenv("GITHUB_TOKEN", secret + "-gh")
    repo, patch = _setup(tmp_path)
    dump = shlex.join([sys.executable, "-c", "import os, sys; print(sorted(os.environ)); sys.exit(1)"])
    code, out, err = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--test-command", dump, "--yes"])
    assert code == 16  # the dump command exits 1 on purpose so its output is shown
    assert "AGNTID_ACCESS_TOKEN" not in out and "GITHUB_TOKEN" not in out
    assert secret not in out and secret not in err


def test_kept_workspace_prints_how_to_remove_it(tmp_path):
    import subprocess

    repo, patch = _setup(tmp_path)
    parent = tmp_path / "ws"
    parent.mkdir()
    code, out, _ = _run(["--repo", str(repo), "--base", "HEAD", "--patch", str(patch), "--yes",
                         "--keep-workspace", "--workspace-dir", str(parent)])
    assert code == 15 and "cleanup:        kept" in out
    commands = [line.strip() for line in out.splitlines() if line.strip().startswith(("git -C", "rmdir "))]
    assert len(commands) == 3
    for command in commands:  # the printed commands really remove it
        subprocess.run(shlex.split(command), check=True)
    assert list(parent.iterdir()) == []
    assert worktree_count(repo) == 1 and patchpilot_branches(repo) == []
