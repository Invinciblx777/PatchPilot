"""Local Git repository checks, patch validation, and a disposable patch workspace.

Everything here is local and offline. It never contacts GitHub or AgntID, and
it can only run these git subcommands (see GIT_SUBCOMMANDS): no commit, push,
fetch, pull, merge, reset, clean, or checkout in the user's working tree.

The original repository is never modified. A patch is applied only inside a
new `git worktree` in a temporary directory outside the repository, on a new
local branch, and that worktree and branch are removed on cleanup.

Patch path rules here are PatchPilot application safeguards. They are not
AgntID enforcement.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# --------------------------------------------------------------------------- #
# Git subprocess boundary
# --------------------------------------------------------------------------- #

# The only git subcommands PatchPilot may run. Nothing that writes history,
# talks to a remote, or rewrites the user's working tree.
GIT_SUBCOMMANDS = frozenset(
    {"rev-parse", "status", "symbolic-ref", "worktree", "branch", "apply", "diff", "check-ref-format"}
)
# Repository hooks and fsmonitor commands are repository-controlled code; never run them.
_GIT_SAFETY = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "--no-optional-locks")
GIT_TIMEOUT_SECONDS = 60


class GitError(Exception):
    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.message = message
        self.returncode = returncode


def _git_env() -> dict[str, str]:
    # GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE in the caller's environment would
    # silently point git at another repository, so every GIT_* variable is dropped.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANGUAGE="C")
    return env


def git(
    cwd: Path, subcommand: str, *args: str, stdin: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """Run one allowlisted git subcommand with an argument list (never a shell)."""
    if subcommand not in GIT_SUBCOMMANDS:
        raise GitError(f"git {subcommand} is not permitted in PatchPilot's local workspace")
    try:
        result = subprocess.run(
            ["git", *_GIT_SAFETY, subcommand, *args],
            cwd=cwd,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            env=_git_env(),
            timeout=GIT_TIMEOUT_SECONDS,
            shell=False,
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {subcommand} did not finish within {GIT_TIMEOUT_SECONDS}s") from None
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit code {result.returncode}"
        raise GitError(f"git {subcommand} failed: {detail}", returncode=result.returncode)
    return result


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


class InvalidRepositoryError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class RepositorySnapshot:
    """Observable state of the original working tree, to prove it was not changed."""

    head: str | None
    branch: str | None
    status: bytes
    dirty_file_hashes: tuple[tuple[str, str], ...]

    @property
    def dirty(self) -> bool:
        return bool(self.status)


@dataclass(frozen=True)
class Repository:
    root: Path
    snapshot: RepositorySnapshot


def _status_entries(status: bytes) -> list[tuple[str, str]]:
    """(XY code, path) pairs from `git status --porcelain=v1 -z`; renames yield two."""
    out: list[tuple[str, str]] = []
    entries = status.split(b"\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code = entry[:2].decode("ascii", "replace")
        out.append((code, entry[3:].decode("utf-8", "surrogateescape")))
        if code[0] in "RC" or code[1] in "RC":
            if i < len(entries) and entries[i]:
                out.append((code, entries[i].decode("utf-8", "surrogateescape")))
            i += 1
    return out


def _status_paths(status: bytes) -> list[str]:
    return [path for _, path in _status_entries(status)]


def snapshot_repository(root: Path) -> RepositorySnapshot:
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    head = git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    hashes = []
    for rel in sorted(set(_status_paths(status))):
        target = root / rel
        if target.is_file() and not target.is_symlink():
            hashes.append((rel, hashlib.sha256(target.read_bytes()).hexdigest()))
    return RepositorySnapshot(
        head=_text(head) if head.returncode == 0 else None,
        branch=_text(branch) if branch.returncode == 0 else None,
        status=status,
        dirty_file_hashes=tuple(hashes),
    )


def open_repository(path: str | os.PathLike[str]) -> Repository:
    """Validate an explicitly supplied path as the root of a non-bare Git working tree."""
    candidate = Path(path)
    if not candidate.is_dir():
        raise InvalidRepositoryError(f"{candidate} is not a directory")
    root = candidate.resolve(strict=True)
    probe = git(root, "rev-parse", "--is-inside-work-tree", "--is-bare-repository", "--show-toplevel", check=False)
    if probe.returncode != 0:
        raise InvalidRepositoryError(f"{root} is not a Git working tree")
    lines = _text(probe).splitlines()
    if len(lines) != 3 or lines[0] != "true" or lines[1] != "false":
        raise InvalidRepositoryError(f"{root} is not a non-bare Git working tree")
    toplevel = Path(lines[2]).resolve()
    if toplevel != root:
        raise InvalidRepositoryError(f"{root} is inside the repository at {toplevel}; pass the repository root")
    return Repository(root=root, snapshot=snapshot_repository(root))


def resolve_base(repo: Repository, revision: str) -> str:
    """Resolve a revision to a full commit ID, or raise InvalidRepositoryError."""
    if not isinstance(revision, str) or not revision.strip() or revision.startswith("-"):
        raise InvalidRepositoryError("base revision must be a non-empty revision name not starting with '-'")
    result = git(
        repo.root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}", check=False
    )
    if result.returncode != 0:
        raise InvalidRepositoryError(f"base revision {revision!r} does not name a commit in {repo.root}")
    return _text(result)


# --------------------------------------------------------------------------- #
# Patch proposal
# --------------------------------------------------------------------------- #

MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_PATCH_FILES = 200

# Default-deny paths: secrets, credentials, and CI/deployment definitions.
# Matched case-insensitively; patterns without "/" match the file name.
DEFAULT_SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.*",
    "*.tfvars",
    ".github/workflows/*",
    ".github/actions/*",
)
# Git control files: never patchable, regardless of policy.
_CONTROL_FILES = frozenset({".gitmodules", ".gitattributes"})
_FORBIDDEN_MODES = {"120000": "symbolic link", "160000": "submodule (gitlink)"}


@dataclass(frozen=True)
class PatchPolicy:
    """Trusted operator configuration. Never built from issue text or patch content."""

    sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
    allowed_sensitive_patterns: tuple[str, ...] = ()

    def sensitive_match(self, path: str) -> str | None:
        """The deny pattern a path matches, unless an explicit allow pattern overrides it."""
        hit = _match(path, self.sensitive_patterns)
        if hit is None or _match(path, self.allowed_sensitive_patterns) is not None:
            return None
        return hit


def _match(path: str, patterns: Iterable[str]) -> str | None:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    for pattern in patterns:
        p = pattern.lower()
        if fnmatch.fnmatchcase(lowered if "/" in p else name, p):
            return pattern
    return None


@dataclass(frozen=True)
class FileChange:
    kind: str  # modify, create, delete, rename, copy
    old_path: str | None
    new_path: str | None
    added: int = 0
    removed: int = 0
    old_mode: str | None = None
    new_mode: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p for p in (self.old_path, self.new_path) if p))

    def describe(self) -> str:
        if self.kind in ("rename", "copy"):
            target = f"{self.old_path} -> {self.new_path}"
        else:
            target = self.new_path or self.old_path or "?"
        mode = f", mode {self.old_mode} -> {self.new_mode}" if self.old_mode and self.new_mode else ""
        if self.kind == "create" and self.new_mode:
            mode = f", mode {self.new_mode}"
        return f"{self.kind:<7} {target} (+{self.added} -{self.removed}{mode})"


@dataclass(frozen=True)
class PatchProposal:
    data: bytes = field(repr=False)
    files: tuple[FileChange, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p for f in self.files for p in f.paths))

    def summary(self) -> str:
        return "\n".join(f.describe() for f in self.files)


class PatchRejectedError(Exception):
    """The patch was rejected before any workspace change. kind: invalid or unsafe."""

    def __init__(self, kind: str, problems: list[str]):
        super().__init__("; ".join(problems))
        self.kind = kind
        self.problems = tuple(problems)


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MODE = re.compile(r"(?:old mode|new mode|deleted file mode|new file mode) ([0-7]{6})")


def _path_problem(path: str) -> str | None:
    """Hard path rules (not configurable). Returns a problem, or None."""
    if not path:
        return "empty path"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or '"' in path or "\\" in path:
        return f"path {path!r} contains quoting, backslashes, or control characters"
    if path.startswith("/") or re.match(r"[A-Za-z]:", path):
        return f"absolute path {path!r}"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return f"path traversal or empty component in {path!r}"
    if any(part.rstrip(". ").lower() == ".git" for part in parts):
        return f"{path!r} is Git metadata"
    if parts[-1].lower() in _CONTROL_FILES:
        return f"{path!r} is a Git control file"
    return None


def _strip_prefix(raw: str, prefix: str, line_no: int, problems: list[str]) -> str | None:
    value = raw.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if '"' in value:
        problems.append(f"line {line_no}: quoted paths are not accepted")
        return None
    if not value.startswith(prefix):
        problems.append(f"line {line_no}: expected a path starting with {prefix!r} (git -p1 format)")
        return None
    return value[len(prefix):]


class _File:
    def __init__(self, header_old: str | None, header_new: str | None, git_style: bool):
        self.git_style = git_style
        self.header_old, self.header_new = header_old, header_new
        self.minus: str | None = None
        self.plus: str | None = None
        self.seen_minus = self.seen_plus = False
        self.rename_from = self.rename_to = None
        self.copy_from = self.copy_to = None
        self.modes: dict[str, str] = {}
        self.hunks = 0
        self.added = self.removed = 0


def parse_patch(data: bytes | str, policy: PatchPolicy) -> PatchProposal:
    """Strictly parse and check a unified diff. Raises PatchRejectedError; never repairs."""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if not raw.strip():
        raise PatchRejectedError("invalid", ["the patch is empty"])
    if len(raw) > MAX_PATCH_BYTES:
        raise PatchRejectedError("invalid", [f"the patch is larger than {MAX_PATCH_BYTES} bytes"])
    if b"\0" in raw:
        raise PatchRejectedError("unsafe", ["the patch contains NUL bytes (binary content)"])
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchRejectedError("invalid", ["the patch is not valid UTF-8"]) from None

    problems: list[str] = []
    files: list[_File] = []
    current: _File | None = None
    old_left = new_left = 0
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        line = lines[i].removesuffix("\r")
        n = i + 1
        i += 1
        if old_left or new_left:  # inside a hunk
            tag = line[:1]
            if line == "" or tag == " ":
                old_left, new_left = old_left - 1, new_left - 1
            elif tag == "-":
                old_left -= 1
                current.removed += 1
            elif tag == "+":
                new_left -= 1
                current.added += 1
            elif tag == "\\":
                pass
            else:
                problems.append(f"line {n}: hunk ends early (unexpected {line[:20]!r})")
                break
            if old_left < 0 or new_left < 0:
                problems.append(f"line {n}: hunk has more lines than its header declares")
                break
            continue
        if line.startswith("\\") and current is not None and current.hunks:
            continue  # "\ No newline at end of file" after the last hunk line
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            if '"' in rest or rest.count(" b/") != 1 or not rest.startswith("a/"):
                problems.append(f"line {n}: unsupported or ambiguous 'diff --git' header")
                break
            a, b = rest.split(" b/", 1)
            current = _File(a[2:], b, git_style=True)
            files.append(current)
            continue
        if line.startswith("--- ") and i < len(lines) and lines[i].startswith("+++ "):
            if current is None or current.seen_minus or current.hunks:
                current = _File(None, None, git_style=False)
                files.append(current)
            current.minus = _strip_prefix(line[4:], "a/", n, problems)
            current.plus = _strip_prefix(lines[i].removesuffix("\r")[4:], "b/", n + 1, problems)
            current.seen_minus = current.seen_plus = True
            i += 1
            continue
        match = _HUNK.match(line)
        if match:
            if current is None or not current.seen_plus:
                problems.append(f"line {n}: hunk without '---'/'+++' file headers")
                break
            old_left = 1 if match.group(2) is None else int(match.group(2))
            new_left = 1 if match.group(4) is None else int(match.group(4))
            current.hunks += 1
            continue
        if current is None:
            continue  # preamble before the first file (for example a commit message)
        if line.startswith(("GIT binary patch", "Binary files ")):
            problems.append(f"line {n}: binary changes are not supported")
            break
        mode = _MODE.match(line)
        if mode:
            current.modes[line.rsplit(" ", 1)[0]] = mode.group(1)
            continue
        for key in ("rename from ", "rename to ", "copy from ", "copy to "):
            if line.startswith(key):
                setattr(current, key.strip().replace(" ", "_"), line[len(key):])
                break
        else:
            if line.startswith(("index ", "similarity index ", "dissimilarity index ")):
                continue
            if line == "-- " and i < len(lines) and re.fullmatch(r"\d+(\.\d+)+.*", lines[i]) and i + 1 == len(lines):
                break  # git format-patch signature at the very end
            problems.append(f"line {n}: unrecognized line outside a hunk: {line[:40]!r}")
            break
    if old_left or new_left:
        problems.append("the patch ends inside a hunk (truncated)")
    if not files and not problems:
        problems.append("no file changes were found in the patch")
    if len(files) > MAX_PATCH_FILES:
        problems.append(f"the patch changes more than {MAX_PATCH_FILES} files")
    if problems:
        raise PatchRejectedError("invalid", problems)

    changes = [_to_change(f, problems) for f in files]
    if problems:
        raise PatchRejectedError("invalid", problems)

    unsafe: list[str] = []
    seen: set[str] = set()
    for change in changes:
        for mode in (change.old_mode, change.new_mode):
            if mode in _FORBIDDEN_MODES:
                unsafe.append(f"{change.new_path or change.old_path}: {_FORBIDDEN_MODES[mode]} changes are not allowed")
        for path in change.paths:
            problem = _path_problem(path)
            if problem:
                unsafe.append(problem)
                continue
            hit = policy.sensitive_match(path)
            if hit:
                unsafe.append(f"{path!r} matches sensitive pattern {hit!r} (default-deny policy)")
            if path in seen and change.kind != "copy":
                unsafe.append(f"{path!r} is changed more than once")
            seen.add(path)
    if unsafe:
        raise PatchRejectedError("unsafe", list(dict.fromkeys(unsafe)))
    return PatchProposal(data=raw, files=tuple(changes))


def _to_change(f: _File, problems: list[str]) -> FileChange:
    deleted = "deleted file mode" in f.modes or (f.seen_plus and f.plus is None)
    created = "new file mode" in f.modes or (f.seen_minus and f.minus is None)
    old = f.rename_from or f.copy_from or f.minus or f.header_old
    new = f.rename_to or f.copy_to or f.plus or f.header_new
    if f.git_style:
        if f.seen_minus and f.minus not in (None, f.header_old) or f.seen_plus and f.plus not in (None, f.header_new):
            problems.append(f"file headers disagree with 'diff --git a/{f.header_old} b/{f.header_new}'")
    if (f.rename_from or f.copy_from) is None and old != new and not (deleted or created):
        problems.append(f"old and new paths differ without a rename: {old!r} -> {new!r}")
    if created and deleted:
        problems.append(f"{new or old!r} is both created and deleted")
    if not f.hunks and not (f.rename_from or f.copy_from or f.modes):
        problems.append(f"{new or old!r} has no changes")
    kind = (
        "rename" if f.rename_from
        else "copy" if f.copy_from
        else "create" if created
        else "delete" if deleted
        else "modify"
    )
    return FileChange(
        kind=kind,
        old_path=None if created else old,
        new_path=None if deleted else new,
        added=f.added,
        removed=f.removed,
        old_mode=f.modes.get("old mode") or f.modes.get("deleted file mode"),
        new_mode=f.modes.get("new mode") or f.modes.get("new file mode"),
    )


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #


class WorkspaceError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CleanupReport:
    status: str  # removed, kept, failed
    errors: tuple[str, ...] = ()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


class Workspace:
    """A disposable `git worktree` of one base commit, on a new local branch."""

    BRANCH_PREFIX = "patchpilot/workspace-"

    def __init__(self, repo: Repository, base_sha: str, temp_dir: Path, path: Path, branch: str):
        self.repo = repo
        self.base_sha = base_sha
        self.temp_dir = temp_dir
        self.path = path
        self.branch = branch
        self.cleanup_report: CleanupReport | None = None

    @classmethod
    def create(cls, repo: Repository, base_sha: str, *, parent_dir: Path | None = None) -> "Workspace":
        if parent_dir is not None and _is_within(parent_dir, repo.root):
            raise WorkspaceError("the workspace directory must be outside the original repository")
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="patchpilot-", dir=parent_dir)).resolve()
        except OSError as exc:
            raise WorkspaceError(f"cannot create a workspace directory: {exc.strerror}") from None
        if _is_within(temp_dir, repo.root):
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise WorkspaceError("the workspace directory must be outside the original repository")
        branch = f"{cls.BRANCH_PREFIX}{base_sha[:12]}-{secrets.token_hex(4)}"
        path = temp_dir / "worktree"
        try:
            git(repo.root, "check-ref-format", "--branch", branch)
            git(repo.root, "worktree", "add", "--quiet", "-b", branch, str(path), base_sha)
        except GitError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise WorkspaceError(f"could not create the workspace: {exc.message}") from None
        return cls(repo, base_sha, temp_dir, path, branch)

    def contains(self, relative: str) -> bool:
        return _is_within(self.path / relative, self.path)

    def cleanup(self, *, keep: bool = False) -> CleanupReport:
        """Remove the worktree, its branch, and the temporary directory. Idempotent."""
        if self.cleanup_report is not None:
            return self.cleanup_report
        if keep:
            self.cleanup_report = CleanupReport("kept")
            return self.cleanup_report
        errors: list[str] = []
        root = self.repo.root
        if self.path.exists():
            try:
                git(root, "worktree", "remove", "--force", str(self.path))
            except GitError as exc:
                errors.append(exc.message)
        try:
            git(root, "worktree", "prune")
        except GitError as exc:
            errors.append(exc.message)
        tip = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}", check=False)
        if tip.returncode == 0:
            if _text(tip) == self.base_sha:  # never delete a branch someone moved
                try:
                    git(root, "branch", "-D", "--", self.branch)
                except GitError as exc:
                    errors.append(exc.message)
            else:
                errors.append(f"branch {self.branch} moved away from the base commit; it was kept")
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            if self.temp_dir.exists():
                errors.append(f"could not remove {self.temp_dir}")
        self.cleanup_report = CleanupReport("failed" if errors else "removed", tuple(errors))
        return self.cleanup_report


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    kind: str  # ok, invalid (git cannot parse it), check_failed (does not apply)
    message: str = ""


def check_patch(ws: Workspace, proposal: PatchProposal) -> CheckResult:
    """Parse with git, compare with PatchPilot's parse, then check it applies to the base."""
    numstat = git(ws.path, "apply", "--numstat", "-z", "-", stdin=proposal.data, check=False)
    if numstat.returncode != 0:
        return CheckResult(False, "invalid", numstat.stderr.decode("utf-8", "replace").strip())
    git_paths = {
        entry.split(b"\t", 2)[2].decode("utf-8", "surrogateescape")
        for entry in numstat.stdout.split(b"\0")
        if entry.count(b"\t") >= 2
    }
    ours = {f.new_path or f.old_path for f in proposal.files}
    if git_paths != ours:
        return CheckResult(
            False, "invalid", f"git and PatchPilot read different file lists: {sorted(git_paths)} vs {sorted(ours)}"
        )
    check = git(ws.path, "apply", "--check", "--index", "-", stdin=proposal.data, check=False)
    if check.returncode != 0:
        return CheckResult(False, "check_failed", check.stderr.decode("utf-8", "replace").strip())
    return CheckResult(True, "ok")


@dataclass(frozen=True)
class ApplyResult:
    diff: str
    diff_stat: str
    changed_paths: tuple[str, ...]


def apply_patch(ws: Workspace, proposal: PatchProposal) -> ApplyResult:
    """Apply atomically (git apply --index; no --reject, --3way, or --recount), then verify."""
    git(ws.path, "apply", "--index", "-", stdin=proposal.data)
    changed = staged_paths(ws)
    expected = set(proposal.paths)
    stray = set(workspace_status_paths(ws)) - expected
    if set(changed) != expected or stray:
        raise WorkspaceError(
            f"the workspace changed unexpectedly: staged {sorted(changed)}, expected {sorted(expected)}, "
            f"other changes {sorted(stray)}"
        )
    outside = [p for p in changed if not ws.contains(p)]
    if outside:
        raise WorkspaceError(f"changed paths resolve outside the workspace: {outside}")
    diff_args = ("--cached", "--no-ext-diff", "--no-textconv", "--no-color", "-M")
    return ApplyResult(
        diff=git(ws.path, "diff", *diff_args).stdout.decode("utf-8", "replace"),
        diff_stat=_text(git(ws.path, "diff", *diff_args, "--stat")),
        changed_paths=tuple(changed),
    )


def staged_paths(ws: Workspace) -> list[str]:
    out = git(ws.path, "diff", "--cached", "--name-only", "--no-renames", "-z").stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def workspace_status_paths(ws: Workspace) -> list[str]:
    return _status_paths(git(ws.path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout)


def unstaged_changes(ws: Workspace) -> list[str]:
    """Paths that differ from the applied patch (for example, written by test commands)."""
    status = git(ws.path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    return list(dict.fromkeys(path for code, path in _status_entries(status) if code[1] != " "))


__all__ = [
    "DEFAULT_SENSITIVE_PATTERNS",
    "GIT_SUBCOMMANDS",
    "ApplyResult",
    "CheckResult",
    "CleanupReport",
    "FileChange",
    "GitError",
    "InvalidRepositoryError",
    "PatchPolicy",
    "PatchProposal",
    "PatchRejectedError",
    "Repository",
    "RepositorySnapshot",
    "Workspace",
    "WorkspaceError",
    "apply_patch",
    "check_patch",
    "git",
    "open_repository",
    "parse_patch",
    "resolve_base",
    "snapshot_repository",
    "unstaged_changes",
]
