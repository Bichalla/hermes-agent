"""Synthetic Git workspace observation tests for Change Gate runtime."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_cli.change_gate import ChangeGateReason
from hermes_cli.change_gate_runtime import (
    WorkspaceChangedPath,
    observe_workspace_changes,
)


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True)
    _run_git(repo, "config", "user.email", "test@example.invalid")
    _run_git(repo, "config", "user.name", "Test Engineer")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "-m", "base")
    return repo


def test_observe_workspace_clean_returns_empty_canonical_observation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    observation = observe_workspace_changes(repo)

    assert observation.reason is ChangeGateReason.ALLOWED
    assert observation.changed_paths == ()
    assert observation.changes == ()


def test_observe_workspace_merges_staged_unstaged_and_untracked_kinds(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tracked = repo / "tracked.txt"
    tracked.write_text("staged\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    tracked.write_text("staged plus unstaged\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    observation = observe_workspace_changes(repo)

    assert observation.reason is ChangeGateReason.ALLOWED
    assert observation.changed_paths == ("new.txt", "tracked.txt")
    assert observation.changes == (
        WorkspaceChangedPath(
            path="new.txt",
            staged=False,
            unstaged=False,
            untracked=True,
        ),
        WorkspaceChangedPath(
            path="tracked.txt",
            staged=True,
            unstaged=True,
            untracked=False,
        ),
    )


def test_observe_workspace_rejects_delete_rename_and_copy_as_ambiguous(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").unlink()

    deleted = observe_workspace_changes(repo)

    assert deleted.reason is ChangeGateReason.ARTIFACT_AMBIGUOUS

    repo = _repo(tmp_path / "rename")
    _run_git(repo, "mv", "tracked.txt", "renamed.txt")

    renamed = observe_workspace_changes(repo)

    assert renamed.reason is ChangeGateReason.ARTIFACT_AMBIGUOUS

    repo = _repo(tmp_path / "copy")
    (repo / "copied.txt").write_text(
        (repo / "tracked.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _run_git(repo, "add", "copied.txt")

    copied = observe_workspace_changes(repo)

    assert copied.reason is ChangeGateReason.ARTIFACT_AMBIGUOUS


def test_observe_workspace_rejects_gitlink_submodule_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _run_git(repo, "rev-parse", "HEAD")
    _run_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},submodule")

    observation = observe_workspace_changes(repo)

    assert observation.reason is ChangeGateReason.ARTIFACT_AMBIGUOUS
