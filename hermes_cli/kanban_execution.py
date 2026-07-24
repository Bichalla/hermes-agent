"""Pure domain contract for one managed Kanban execution attempt.

This module deliberately contains no process, database, gateway, or Codex
infrastructure.  It owns only the approved-spec boundary, immutable process
identity observations, and the single replaceable execution Port.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


APPROVED_SPEC_SCHEMA = "kanban-approved-execution-spec/v1"
PILOT_BACKEND = "codex-direct/v1"
PILOT_BOARD = "lifelog-control"
PILOT_ASSIGNEE = "codex-direct"
PILOT_TIMEOUT_SECONDS = 1800

_SPEC_KEYS = frozenset(
    {
        "assignee",
        "backend",
        "board",
        "branch_name",
        "instructions",
        "max_runtime_seconds",
        "project_id",
        "repository_root",
        "schema",
        "task_id",
        "title",
        "worktree_path",
    }
)
_TASK_ID_RE = re.compile(r"t_[0-9a-f]{8}\Z")


class ExecutionState(str, Enum):
    """Closed outcomes of a non-mutating process observation."""

    RUNNING = "running"
    EXITED = "exited"
    IDENTITY_MISMATCH = "identity_mismatch"


class TerminationState(str, Enum):
    """Closed outcomes of identity-aware bounded termination."""

    DEAD = "dead"
    IDENTITY_MISMATCH = "identity_mismatch"
    SURVIVOR = "survivor"


def _require_builtin_string(name: str, value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ValueError("invalid approved execution spec")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ValueError("invalid approved execution spec") from None
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("invalid approved execution spec")
    if not allow_empty and not value:
        raise ValueError("invalid approved execution spec")
    return value


def _require_canonical_absolute_path(name: str, value: object) -> Path:
    text = _require_builtin_string(name, value)
    path = Path(text)
    if text != str(path):
        raise ValueError("invalid approved execution spec")
    if not path.is_absolute():
        raise ValueError("invalid approved execution spec")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("invalid approved execution spec") from None
    if path != resolved:
        # This rejects lexical aliases ('.'/'..') and every existing symlink
        # component while still permitting the not-yet-created worktree leaf.
        raise ValueError("invalid approved execution spec")
    return path


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid approved execution spec")
        result[key] = value
    return result


def _reject_non_json_constant(_value: str):
    raise ValueError("invalid approved execution spec")


@dataclass(frozen=True)
class ApprovedExecutionSpec:
    """The exact immutable host-approved input for one managed attempt."""

    assignee: str
    backend: str
    board: str
    branch_name: str
    instructions: str
    max_runtime_seconds: int
    project_id: str
    repository_root: str
    schema: str
    task_id: str
    title: str
    worktree_path: str

    def __post_init__(self) -> None:
        for name in (
            "assignee",
            "board",
            "branch_name",
            "instructions",
            "project_id",
            "repository_root",
            "schema",
            "task_id",
            "title",
            "worktree_path",
        ):
            _require_builtin_string(name, getattr(self, name))

        _require_builtin_string("backend", self.backend)
        if type(self.max_runtime_seconds) is not int:
            raise ValueError("invalid approved execution spec")
        if self.schema != APPROVED_SPEC_SCHEMA:
            raise ValueError("invalid approved execution spec")
        if self.backend != PILOT_BACKEND:
            raise ValueError("invalid approved execution spec")
        if self.board != PILOT_BOARD:
            raise ValueError("invalid approved execution spec")
        if self.assignee != PILOT_ASSIGNEE:
            raise ValueError("invalid approved execution spec")
        if self.max_runtime_seconds != PILOT_TIMEOUT_SECONDS:
            raise ValueError("invalid approved execution spec")
        if _TASK_ID_RE.fullmatch(self.task_id) is None:
            raise ValueError("invalid approved execution spec")

        repository_root = _require_canonical_absolute_path(
            "repository_root", self.repository_root
        )
        worktree_path = _require_canonical_absolute_path(
            "worktree_path", self.worktree_path
        )
        expected_worktree = repository_root / ".worktrees" / self.task_id
        if worktree_path != expected_worktree:
            raise ValueError("invalid approved execution spec")

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> "ApprovedExecutionSpec":
        """Parse only exact canonical UTF-8 JSON bytes for the closed schema."""

        if type(raw) is not bytes:
            raise ValueError("invalid approved execution spec")
        try:
            text = raw.decode("utf-8", errors="strict")
            payload = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_json_constant,
            )
        except (
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
            TypeError,
        ):
            raise ValueError("invalid approved execution spec") from None

        if type(payload) is not dict or set(payload) != _SPEC_KEYS:
            raise ValueError("invalid approved execution spec")
        if any(type(key) is not str for key in payload):
            raise ValueError("invalid approved execution spec")

        backend_value = payload["backend"]
        if type(backend_value) is not str:
            raise ValueError("invalid approved execution spec")
        try:
            spec = cls(
                assignee=payload["assignee"],
                backend=backend_value,
                board=payload["board"],
                branch_name=payload["branch_name"],
                instructions=payload["instructions"],
                max_runtime_seconds=payload["max_runtime_seconds"],
                project_id=payload["project_id"],
                repository_root=payload["repository_root"],
                schema=payload["schema"],
                task_id=payload["task_id"],
                title=payload["title"],
                worktree_path=payload["worktree_path"],
            )
            canonical = spec.to_canonical_bytes()
        except (UnicodeError, ValueError, TypeError):
            raise ValueError("invalid approved execution spec") from None
        if canonical != raw:
            raise ValueError("invalid approved execution spec")
        return spec

    def to_canonical_bytes(self) -> bytes:
        """Return canonical UTF-8 JSON with no trailing line feed."""

        payload = {
            "assignee": self.assignee,
            "backend": self.backend,
            "board": self.board,
            "branch_name": self.branch_name,
            "instructions": self.instructions,
            "max_runtime_seconds": self.max_runtime_seconds,
            "project_id": self.project_id,
            "repository_root": self.repository_root,
            "schema": self.schema,
            "task_id": self.task_id,
            "title": self.title,
            "worktree_path": self.worktree_path,
        }
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True)
class ExecutionHandle:
    """Complete host-owned identity for one prepared process session."""

    backend: str
    pid: int
    pgid: int
    kernel_start_time: int

    def __post_init__(self) -> None:
        if type(self.backend) is not str or self.backend != PILOT_BACKEND:
            raise ValueError("invalid execution handle")
        for value in (self.pid, self.pgid, self.kernel_start_time):
            if type(value) is not int or value <= 0:
                raise ValueError("invalid execution handle")


@dataclass(frozen=True)
class ExecutionObservation:
    """Closed observation of a prepared or released execution."""

    state: ExecutionState
    exit_code: int | None

    def __post_init__(self) -> None:
        if type(self.state) is not ExecutionState:
            raise ValueError("invalid execution observation")
        if self.state is ExecutionState.EXITED:
            if type(self.exit_code) is not int:
                raise ValueError("invalid execution observation")
        elif self.exit_code is not None:
            raise ValueError("invalid execution observation")


@dataclass(frozen=True)
class TerminationObservation:
    """Closed result of identity-aware termination and readback."""

    state: TerminationState

    def __post_init__(self) -> None:
        if type(self.state) is not TerminationState:
            raise ValueError("invalid termination observation")


class ExecutionBackend(Protocol):
    """The sole replaceable Port at the managed-execution boundary."""

    kind: str

    def prepare(
        self, spec: ApprovedExecutionSpec, worktree: Path
    ) -> ExecutionHandle: ...

    def release(self, handle: ExecutionHandle) -> None: ...

    def observe(self, handle: ExecutionHandle) -> ExecutionObservation: ...

    def terminate(self, handle: ExecutionHandle) -> TerminationObservation: ...
