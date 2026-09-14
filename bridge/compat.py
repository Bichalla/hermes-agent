"""Compatibility checks for the Hermes Kanban approval bridge overlay.

The bridge lives outside the protected Hermes runtime.  This module validates a
    prepared local Hermes source tree before any release candidate is built from it:
    only a clean committed descendant, exact reviewed file hashes, and known
    startup/API surfaces are accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WRITE_SURFACE_RE = re.compile(
    r"sqlite3|CREATE\s+TABLE|ALTER\s+TABLE|executescript|write_text|write_bytes|"
    r"json\.dump|yaml\.(safe_)?dump",
    re.IGNORECASE,
)


class CompatError(RuntimeError):
    """Raised when the candidate source is not safe to prepare."""


@dataclass(frozen=True)
class ValidationReport:
    """Successful compatibility validation result."""

    source: Path
    head: str
    active_head: str | None
    overlay_files: tuple[str, ...]
    scanner_sensitive_files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "compatible",
            "source": str(self.source),
            "head": self.head,
            "active_head": self.active_head,
            "overlay_files": list(self.overlay_files),
            "scanner_sensitive_files": list(self.scanner_sensitive_files),
        }


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a bridge compatibility manifest."""

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompatError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CompatError(f"manifest is not valid JSON: {path}: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise CompatError("unsupported compatibility manifest schema")
    return manifest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_git_blob(repo: Path, ref: str, rel: str) -> str | None:
    result = _git(repo, "show", f"{ref}:{rel}", check=False, text=False)
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def validate_candidate_source(
    source: Path,
    manifest: dict[str, Any],
    *,
    active_source: Path | None = None,
) -> ValidationReport:
    """Validate that *source* is exactly the reviewed bridge overlay.

    The local source must be a clean checkout of a committed bridge patch. Its
    diff from the reviewed active/base commit must contain exactly the manifest's
    overlay files; dirty edits, deletions, renamed paths, unknown files, or
    API/hash drift fail closed with an actionable message.
    """

    source = source.resolve()
    if not (source / ".git").exists():
        raise CompatError(f"candidate source is not a git checkout: {source}")

    head = git_head(source)
    supported_heads = tuple(manifest.get("supported_heads") or ())
    if head not in supported_heads:
        raise CompatError(f"unsupported Hermes source head: {head}")
    if _dirty_paths(source):
        raise CompatError("candidate source must be clean; commit the reviewed overlay first")
    base_ref = _require_base_ref(manifest)

    active_head: str | None = None
    if active_source is not None:
        active_source = active_source.resolve()
        active_head = git_head(active_source)
        if active_head != manifest.get("active_head"):
            raise CompatError(f"active Hermes head changed: {active_head}")
        if active_head != base_ref:
            raise CompatError(f"manifest base {base_ref} does not match active head {active_head}")
        if not is_ancestor(source, active_head, head):
            raise CompatError(f"candidate head {head} is not a descendant of active head {active_head}")

    overlays = _overlay_entries(manifest)
    expected_paths = set(overlays)
    actual_paths = set(_diff_paths(source, base_ref, head))
    if actual_paths != expected_paths:
        raise CompatError(
            "candidate overlay file set differs from manifest: "
            f"expected={sorted(expected_paths)} actual={sorted(actual_paths)}"
        )

    for rel, entry in overlays.items():
        _validate_overlay_file(source, base_ref, head, rel, entry)

    _validate_required_hashes(source, base_ref, manifest)
    _validate_required_snippets(source, manifest)

    sensitive = tuple(
        rel for rel in sorted(expected_paths)
        if (source / rel).is_file() and WRITE_SURFACE_RE.search((source / rel).read_text(errors="replace"))
    )
    allowed_sensitive = tuple(sorted(manifest.get("allowed_scanner_sensitive_files") or ()))
    if sensitive != allowed_sensitive:
        raise CompatError(
            "candidate scanner-sensitive file set differs from manifest: "
            f"expected={list(allowed_sensitive)} actual={list(sensitive)}"
        )

    return ValidationReport(
        source=source,
        head=head,
        active_head=active_head,
        overlay_files=tuple(sorted(expected_paths)),
        scanner_sensitive_files=sensitive,
    )


def git_head(repo: Path) -> str:
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if not FULL_SHA_RE.fullmatch(head):
        raise CompatError(f"git HEAD is not a full commit SHA: {repo}")
    return head


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def _overlay_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = manifest.get("overlay_files")
    if not isinstance(raw, dict) or not raw:
        raise CompatError("manifest.overlay_files must be a non-empty object")
    entries: dict[str, dict[str, Any]] = {}
    for rel, entry in raw.items():
        if not _valid_relative_path(rel):
            raise CompatError(f"unsafe overlay path in manifest: {rel!r}")
        if not isinstance(entry, dict):
            raise CompatError(f"overlay entry must be an object: {rel}")
        entries[rel] = entry
    return entries


def _valid_relative_path(rel: str) -> bool:
    path = Path(rel)
    return isinstance(rel, str) and rel and not path.is_absolute() and ".." not in path.parts


def _require_base_ref(manifest: dict[str, Any]) -> str:
    base_ref = manifest.get("base_ref") or manifest.get("active_head")
    if not isinstance(base_ref, str) or not FULL_SHA_RE.fullmatch(base_ref):
        raise CompatError("manifest.base_ref must be a full commit SHA")
    return base_ref


def _dirty_paths(repo: Path) -> list[str]:
    output = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    return [line for line in output.splitlines() if line]


def _diff_paths(repo: Path, base_ref: str, head: str) -> list[str]:
    output = _git(repo, "diff", "--name-only", "--diff-filter=ACMRT", f"{base_ref}..{head}").stdout
    paths = [line for line in output.splitlines() if line]
    for rel in paths:
        if not _valid_relative_path(rel):
            raise CompatError(f"unsafe diff path: {rel!r}")
    deleted = _git(repo, "diff", "--name-only", "--diff-filter=D", f"{base_ref}..{head}").stdout.splitlines()
    if deleted:
        raise CompatError(f"candidate overlay may not delete files: {deleted}")
    return paths


def _validate_overlay_file(repo: Path, base_ref: str, head: str, rel: str, entry: dict[str, Any]) -> None:
    path = repo / rel
    if entry.get("state") == "added":
        base_hash = sha256_git_blob(repo, base_ref, rel)
        if base_hash is not None:
            raise CompatError(f"overlay file is marked added but exists in base: {rel}")
    elif entry.get("state") == "modified":
        expected_base = entry.get("base_sha256")
        if not isinstance(expected_base, str):
            raise CompatError(f"modified overlay file missing base_sha256: {rel}")
        base_hash = sha256_git_blob(repo, base_ref, rel)
        if base_hash != expected_base:
            raise CompatError(f"base hash drift for {rel}: {base_hash}")
    else:
        raise CompatError(f"unsupported overlay state for {rel}: {entry.get('state')!r}")

    if not path.is_file() or path.is_symlink():
        raise CompatError(f"overlay path must be a regular file: {rel}")
    expected_patched = entry.get("patched_sha256")
    actual_patched = sha256_git_blob(repo, head, rel)
    if actual_patched != expected_patched or sha256_file(path) != expected_patched:
        raise CompatError(f"patched hash drift for {rel}")


def _validate_required_hashes(repo: Path, head: str, manifest: dict[str, Any]) -> None:
    for rel, expected in (manifest.get("required_base_hashes") or {}).items():
        if not _valid_relative_path(rel):
            raise CompatError(f"unsafe required hash path: {rel!r}")
        actual = sha256_git_blob(repo, head, rel)
        if actual != expected:
            raise CompatError(f"required base hash drift for {rel}: {actual}")


def _validate_required_snippets(repo: Path, manifest: dict[str, Any]) -> None:
    for rel, snippets in (manifest.get("required_snippets") or {}).items():
        if not _valid_relative_path(rel):
            raise CompatError(f"unsafe required snippet path: {rel!r}")
        text = (repo / rel).read_text(errors="replace")
        for snippet in snippets:
            if not isinstance(snippet, str) or snippet not in text:
                raise CompatError(f"required snippet missing from {rel}: {snippet!r}")


def _git(repo: Path, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        timeout=60,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        raise CompatError(f"git {' '.join(args)} failed in {repo}: {stderr}")
    return result
