"""Patch check/apply runs and the repositories view, on real temporary Git repositories.

Everything is local: real git, real subprocesses, no network, no GitHub, no
AgntID. Patches come from real `git diff` output (tests/patch_helpers.py).
"""

from __future__ import annotations

import hashlib
import sys
import time

import pytest

from patchpilot.patch_verification import TestCommand

from .patch_helpers import FIX, WRONG_FIX, make_patch, make_repo, patchpilot_branches, sh_git, worktree_count
from .web_helpers import last_event, make_settings, serve_web, statuses

PYTEST = TestCommand("unit", (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"), timeout_seconds=120)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _setup(tmp_path, edits=FIX, tests=(PYTEST,)):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, edits)
    return repo, patch, make_settings(repos={"r1": repo}, tests=tests)


def _apply(web, patch, *, tests=True, digest=None, **extra):
    return web.run("patch_apply", repoId="r1", base="main", patch=patch,
                   patchSha256=digest or _sha(patch), approveApply=True, approveTests=tests, **extra)


def test_check_reports_parsed_changes_and_a_digest_and_applies_nothing(tmp_path):
    repo, patch, settings = _setup(tmp_path)
    with serve_web(settings) as web:
        run = web.run("patch_check", repoId="r1", base="main", patch=patch)
    result = run["result"]
    assert run["outcome"] == "check_passed"
    assert result["patchSha256"] == _sha(patch)
    assert result["fileChanges"] == [{
        "kind": "modify", "oldPath": "calc.py", "newPath": "calc.py",
        "added": 1, "removed": 1, "oldMode": None, "newMode": None,
    }]
    assert result["patchApplied"] is False and result["diff"] == ""
    assert [t["status"] for t in result["tests"]] == ["not_run"]
    assert statuses(run) == [("patch", "info"), ("patch", "ok")]
    assert last_event(run, "patch")["source"] == "patchpilot"
    assert result["cleanup"]["status"] == "removed" and result["originalPreserved"] is True
    assert worktree_count(repo) == 1 and patchpilot_branches(repo) == []


def test_apply_runs_configured_tests_only_after_approval(tmp_path):
    repo, patch, settings = _setup(tmp_path)
    with serve_web(settings) as web:
        run = _apply(web, patch)
    result = run["result"]
    assert run["outcome"] == "tests_passed"
    assert "+    return a + b" in result["diff"] and result["changedPaths"] == ["calc.py"]
    test = result["tests"][0]
    assert test["status"] == "passed" and test["exitCode"] == 0 and test["durationSeconds"] > 0
    steps = statuses(run)
    assert steps[0] == ("patch", "info")
    assert ("apply", "info") in steps and ("tests", "info") in steps and ("tests", "ok") in steps
    assert steps[-1] == ("patch", "ok")
    assert [e["detail"]["approved"] for e in run["events"] if e["step"] in ("apply", "tests") and "approved" in e["detail"]] == [True, True]
    assert result["cleanup"]["status"] == "removed" and result["originalPreserved"] is True
    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"  # the original is untouched


def test_declined_tests_are_skipped_not_passed(tmp_path):
    _, patch, settings = _setup(tmp_path)
    with serve_web(settings) as web:
        run = _apply(web, patch, tests=False)
    assert run["outcome"] == "patch_applied"
    assert [t["status"] for t in run["result"]["tests"]] == ["skipped"]


def test_failing_tests_are_reported_as_failed(tmp_path):
    _, patch, settings = _setup(tmp_path, WRONG_FIX)
    with serve_web(settings) as web:
        run = _apply(web, patch)
    test = run["result"]["tests"][0]
    assert run["outcome"] == "tests_failed"
    assert test["status"] == "failed" and test["exitCode"] == 1 and "assert 6 == 5" in test["output"]
    assert ("tests", "error") in statuses(run) and statuses(run)[-1] == ("patch", "error")


def test_apply_is_bound_to_the_reviewed_patch(tmp_path):
    repo, patch, settings = _setup(tmp_path)
    other = make_patch(repo, WRONG_FIX)
    with serve_web(settings) as web:
        changed = web.start("patch_apply", repoId="r1", base="main", patch=other,
                            patchSha256=_sha(patch), approveApply=True, approveTests=True)
        unapproved = web.start("patch_apply", repoId="r1", base="main", patch=patch,
                               patchSha256=_sha(patch), approveApply=False, approveTests=True)
        runs = web.get("/api/runs").json()["runs"]
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "patch_changed"
    assert unapproved.status_code == 400
    assert runs == []


def test_unsafe_patch_is_rejected_and_the_policy_cannot_be_relaxed_by_a_request(tmp_path):
    repo, _, settings = _setup(tmp_path)
    patch = make_patch(repo, {".env": "TOKEN=x\n"})
    with serve_web(settings) as web:
        run = web.run("patch_check", repoId="r1", base="main", patch=patch)
        relaxed = web.start("patch_check", repoId="r1", base="main", patch=patch, allowSensitivePath=[".env"])
    assert run["outcome"] == "unsafe_patch"
    assert any("sensitive pattern" in p for p in run["result"]["problems"])
    assert run["result"]["fileChanges"] is None
    assert relaxed.status_code == 400


@pytest.mark.parametrize("body", [{"repoId": "r9"}, {"repoId": "r1", "repoPath": "/tmp"}, {"repoId": "r1", "tests": []}])
def test_requests_cannot_name_other_repositories_or_commands(tmp_path, body):
    _, patch, settings = _setup(tmp_path)
    with serve_web(settings) as web:
        response = web.start("patch_check", base="main", patch=patch, **body)
    assert response.status_code == 400


def test_uncommitted_changes_need_an_explicit_allowance(tmp_path):
    repo, patch, settings = _setup(tmp_path)
    (repo / "README.md").write_text("# local edit\n")
    with serve_web(settings) as web:
        refused = web.run("patch_check", repoId="r1", base="main", patch=patch)
        allowed = web.run("patch_check", repoId="r1", base="main", patch=patch, allowUncommitted=True)
    assert refused["outcome"] == "uncommitted_changes"
    assert last_event(refused, "patch")["status"] == "warning"
    assert allowed["outcome"] == "check_passed"
    assert (repo / "README.md").read_text() == "# local edit\n"


def test_only_one_patch_run_at_a_time(tmp_path):
    slow = TestCommand("slow", (sys.executable, "-c", "import time; time.sleep(2)"), timeout_seconds=30)
    _, patch, settings = _setup(tmp_path, tests=(slow,))
    with serve_web(settings) as web:
        first = web.start("patch_apply", repoId="r1", base="main", patch=patch,
                          patchSha256=_sha(patch), approveApply=True, approveTests=True)
        time.sleep(0.3)
        second = web.start("patch_check", repoId="r1", base="main", patch=patch)
        _, final = web.follow(first.json()["run"]["id"])
    assert second.status_code == 409 and second.json()["error"]["code"] == "busy"
    assert final["outcome"] == "tests_passed"


def test_repositories_view_reports_live_read_only_state(tmp_path):
    repo, _, _ = _setup(tmp_path)
    not_git = tmp_path / "plain"
    not_git.mkdir()
    head = sh_git(repo, "rev-parse", "HEAD").strip()
    settings = make_settings(repos={"r1": repo, "r2": not_git})
    with serve_web(settings) as web:
        clean = web.get("/api/repos").json()["repos"]
        (repo / "README.md").write_text("# local edit\n")
        dirty = web.get("/api/repos").json()["repos"]
    assert clean[0] == {
        "id": "r1", "name": "repo", "path": str(repo.resolve()),
        "ok": True, "branch": "main", "head": head, "dirty": False, "changedPaths": 0,
    }
    assert clean[1]["ok"] is False and "not a Git working tree" in clean[1]["error"]
    assert dirty[0]["dirty"] is True and dirty[0]["changedPaths"] == 1
