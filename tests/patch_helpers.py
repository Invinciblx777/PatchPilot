"""Real temporary Git repositories for the Module 4 tests.

Each repository is created with the real `git` binary in pytest's tmp_path.
Patches are produced by real `git diff` from real edits, then the edit is
reverted, so every patch is a genuine unified diff of that repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_ID = ("-c", "user.name=PatchPilot Tests", "-c", "user.email=tests@patchpilot.invalid", "-c", "commit.gpgsign=false")


def sh_git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout


def make_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    repo = root / "repo"
    repo.mkdir()
    sh_git(repo, "init", "-q", "-b", "main")
    files = files or {
        "calc.py": "def add(a, b):\n    return a - b\n",
        "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        "README.md": "# demo\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    sh_git(repo, "add", "-A")
    sh_git(repo, "commit", "-q", "-m", "initial")
    return repo


def commit_all(repo: Path, message: str) -> str:
    sh_git(repo, "add", "-A")
    sh_git(repo, "commit", "-q", "-m", message)
    return sh_git(repo, "rev-parse", "HEAD").strip()


def make_patch(repo: Path, edits: dict[str, str | None]) -> str:
    """Apply edits (None deletes), capture `git diff` including new files, then revert."""
    for name, content in edits.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    sh_git(repo, "add", "-A")
    patch = sh_git(repo, "diff", "--cached", "--no-color")
    sh_git(repo, "reset", "-q", "--hard")  # test helper only; PatchPilot never resets
    sh_git(repo, "clean", "-q", "-fd")
    return patch


def patchpilot_branches(repo: Path) -> list[str]:
    out = sh_git(repo, "branch", "--list", "patchpilot/*", "--format=%(refname:short)")
    return [line for line in out.splitlines() if line]


def worktree_count(repo: Path) -> int:
    return sh_git(repo, "worktree", "list", "--porcelain").count("worktree ")


FIX = {"calc.py": "def add(a, b):\n    return a + b\n"}
WRONG_FIX = {"calc.py": "def add(a, b):\n    return a * b\n"}
