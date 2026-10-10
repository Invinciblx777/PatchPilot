"""Strict validation of browser requests.

Bodies with unknown fields are rejected, so a request can never carry a test
command, a path policy, or a repository path: those exist only in operator
configuration (see web/config.py). These are input-hygiene checks made by
PatchPilot; the AgntID runtime and the GitHub MCP server stay the authorities
for what a call may do.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from patchpilot.patch_workspace import MAX_PATCH_BYTES
from patchpilot.task_session import is_reserved_argument

MAX_PROMPT_CHARS = 2000
MAX_FILES = 5


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.problems = problems or []

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "problems": self.problems}}


def invalid(message: str, problems: list[str] | None = None) -> RequestError:
    return RequestError(400, "invalid_request", message, problems)


def _object(
    value: Any, label: str, required: set[str], optional: set[str] | frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise invalid(f"{label} must be a JSON object")
    missing = required - set(value)
    unknown = set(value) - required - set(optional)
    if missing or unknown:
        problems = [f"missing field {name!r}" for name in sorted(missing)]
        problems += [f"unknown field {name!r}" for name in sorted(unknown)]
        raise invalid(f"{label} has missing or unknown fields", problems)
    return value


def _text(value: Any, label: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid(f"{label} must be a non-empty string")
    if len(value) > max_chars:
        raise invalid(f"{label} is longer than {max_chars} characters")
    return value.strip()


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise invalid(f"{label} must be true or false")
    return value


# --------------------------------------------------------------------------- #
# Issue references
# --------------------------------------------------------------------------- #

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_SHORT_FORM = re.compile(rf"({_OWNER})/({_REPO})#([0-9]{{1,10}})")
_URL_FORM = re.compile(rf"https://github\.com/({_OWNER})/({_REPO})/issues/([0-9]{{1,10}})/?(?:[?#].*)?")
_MAX_ISSUE_NUMBER = 2**31 - 1


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def reference(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    def view(self) -> dict[str, Any]:
        return {"owner": self.owner, "repo": self.repo, "number": self.number, "reference": self.reference}


def parse_issue_reference(value: Any) -> IssueRef:
    text = value.strip() if isinstance(value, str) else ""
    match = _URL_FORM.fullmatch(text) or _SHORT_FORM.fullmatch(text)
    if match is None:
        raise invalid("issue must be https://github.com/OWNER/REPO/issues/N or OWNER/REPO#N")
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    if repo in (".", "..") or not 1 <= number <= _MAX_ISSUE_NUMBER:
        raise invalid("issue must name a repository and an issue number of 1 or more")
    return IssueRef(owner, repo, number)


# --------------------------------------------------------------------------- #
# Run requests
# --------------------------------------------------------------------------- #

_REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")


@dataclass(frozen=True)
class FileRequest:
    path: str
    ref: str | None = None

    def view(self) -> dict[str, Any]:
        return {"path": self.path, "ref": self.ref}


def _file(value: Any, index: int) -> FileRequest:
    entry = _object(value, f"files[{index}]", {"path"}, {"ref"})
    path = entry["path"]
    if not isinstance(path, str) or not 0 < len(path) <= 300:
        raise invalid(f"files[{index}].path must be a repository-relative path of 1 to 300 characters")
    parts = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise invalid(f"files[{index}].path must be relative, without '..', empty parts, or backslashes")
    ref = entry.get("ref")
    if ref is not None and (
        not isinstance(ref, str) or not _REF.fullmatch(ref) or ref.startswith("-") or ".." in ref
    ):
        raise invalid(f"files[{index}].ref must be a branch, tag, or commit name")
    return FileRequest(path, ref)


@dataclass(frozen=True)
class InvestigationRequest:
    issue: IssueRef
    prompt: str
    files: tuple[FileRequest, ...] = ()

    def view(self) -> dict[str, Any]:
        return {"issue": self.issue.view(), "prompt": self.prompt, "files": [f.view() for f in self.files]}


def investigation_request(body: Mapping[str, Any]) -> InvestigationRequest:
    body = _object(body, "investigation request", {"kind", "issue", "prompt"}, {"files"})
    files = body.get("files", [])
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise invalid(f"files must be a list of at most {MAX_FILES} entries")
    return InvestigationRequest(
        issue=parse_issue_reference(body["issue"]),
        prompt=_text(body["prompt"], "prompt", max_chars=MAX_PROMPT_CHARS),
        files=tuple(_file(entry, i) for i, entry in enumerate(files)),
    )


# Shown instead of a reserved argument's value, which may be approval material.
WITHHELD = "[withheld: reserved AgntID argument]"


@dataclass(frozen=True)
class ProbeCall:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict, repr=False)
    confirm_unannotated: bool = False

    def arguments_view(self) -> dict[str, Any]:
        """The arguments for display: reserved names kept, their values withheld."""
        return {k: WITHHELD if is_reserved_argument(k) else v for k, v in self.arguments.items()}


@dataclass(frozen=True)
class ProbeRequest:
    prompt: str
    call: ProbeCall | None = None

    def view(self) -> dict[str, Any]:
        call = None
        if self.call is not None:
            call = {
                "tool": self.call.tool,
                "arguments": self.call.arguments_view(),
                "confirmUnannotated": self.call.confirm_unannotated,
            }
        return {"prompt": self.prompt, "call": call}


def probe_request(body: Mapping[str, Any]) -> ProbeRequest:
    body = _object(body, "probe request", {"kind", "prompt"}, {"call"})
    call = body.get("call")
    if call is not None:
        call = _object(call, "call", {"tool", "arguments", "confirmUnannotated"})
        if not isinstance(call["arguments"], Mapping):
            raise invalid("call.arguments must be a JSON object")
        call = ProbeCall(
            tool=_text(call["tool"], "call.tool", max_chars=200),
            arguments=dict(call["arguments"]),
            confirm_unannotated=_bool(call["confirmUnannotated"], "call.confirmUnannotated"),
        )
    return ProbeRequest(prompt=_text(body["prompt"], "prompt", max_chars=MAX_PROMPT_CHARS), call=call)


@dataclass(frozen=True)
class PatchRequest:
    repo_id: str
    base: str
    patch: str = field(repr=False)
    allow_uncommitted: bool = False
    patch_sha256: str | None = None
    approve_apply: bool = False
    approve_tests: bool = False

    def view(self) -> dict[str, Any]:
        return {
            "repoId": self.repo_id,
            "base": self.base,
            "patchBytes": len(self.patch.encode("utf-8")),
            "allowUncommitted": self.allow_uncommitted,
            "patchSha256": self.patch_sha256,
            "approveApply": self.approve_apply,
            "approveTests": self.approve_tests,
        }


def _patch_fields(body: Mapping[str, Any]) -> tuple[str, str, str, bool]:
    repo_id = body["repoId"]
    if not isinstance(repo_id, str) or not repo_id:
        raise invalid("repoId must name a configured repository")
    base = _text(body["base"], "base", max_chars=256)
    patch = body["patch"]
    if not isinstance(patch, str) or not patch.strip():
        raise invalid("patch must be a non-empty unified diff")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise invalid(f"patch is larger than {MAX_PATCH_BYTES} bytes")
    return repo_id, base, patch, _bool(body.get("allowUncommitted", False), "allowUncommitted")


def patch_check_request(body: Mapping[str, Any]) -> PatchRequest:
    body = _object(body, "patch check request", {"kind", "repoId", "base", "patch"}, {"allowUncommitted"})
    repo_id, base, patch, allow_uncommitted = _patch_fields(body)
    return PatchRequest(repo_id, base, patch, allow_uncommitted)


def patch_apply_request(body: Mapping[str, Any]) -> PatchRequest:
    body = _object(
        body,
        "patch apply request",
        {"kind", "repoId", "base", "patch", "patchSha256", "approveApply", "approveTests"},
        {"allowUncommitted"},
    )
    repo_id, base, patch, allow_uncommitted = _patch_fields(body)
    digest = body["patchSha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise invalid("patchSha256 must be the 64-hex digest returned by the patch check")
    if _bool(body["approveApply"], "approveApply") is not True:
        raise invalid("approveApply must be true; use a patch check to validate without applying")
    return PatchRequest(
        repo_id, base, patch, allow_uncommitted,
        patch_sha256=digest, approve_apply=True, approve_tests=_bool(body["approveTests"], "approveTests"),
    )
