import ast
import inspect
import json
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path

import pytest

from hermes_cli.kanban_execution import (
    ApprovedExecutionSpec,
    ExecutionBackend,
    ExecutionHandle,
    ExecutionObservation,
    ExecutionState,
    TerminationObservation,
    TerminationState,
)


SCHEMA = "kanban-approved-execution-spec/v1"
BACKEND = "codex-direct/v1"
BOARD = "lifelog-control"
ASSIGNEE = "codex-direct"
TIMEOUT = 1800


def _payload(repo_path: Path, **changes):
    task_id = "t_1234abcd"
    payload = {
        "assignee": ASSIGNEE,
        "backend": BACKEND,
        "board": BOARD,
        "branch_name": "lifelog-control/t_1234abcd-기록-정리",
        "instructions": "구현 지시",
        "max_runtime_seconds": TIMEOUT,
        "project_id": "project-123",
        "repository_root": str(repo_path),
        "schema": SCHEMA,
        "task_id": task_id,
        "title": "기록 정리",
        "worktree_path": str(repo_path / ".worktrees" / task_id),
    }
    payload.update(changes)
    return payload


def _canonical(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse(payload):
    return ApprovedExecutionSpec.from_canonical_bytes(_canonical(payload))


def test_approved_execution_spec_golden_canonical_vector(tmp_path):
    repository_root = tmp_path / "저장소"
    repository_root.mkdir()
    payload = _payload(repository_root)
    raw = _canonical(payload)

    spec = ApprovedExecutionSpec.from_canonical_bytes(raw)

    assert spec.schema == SCHEMA
    assert type(spec.backend) is str
    assert spec.backend == BACKEND
    assert spec.board == BOARD
    assert spec.assignee == ASSIGNEE
    assert spec.max_runtime_seconds == TIMEOUT
    assert spec.repository_root == str(repository_root)
    assert spec.worktree_path == str(repository_root / ".worktrees" / spec.task_id)
    assert spec.to_canonical_bytes() == raw
    assert not raw.endswith(b"\n")
    assert b"\\u" not in raw


@pytest.mark.parametrize("missing", sorted(_payload(Path("/repo"))))
def test_spec_rejects_every_missing_key(missing):
    payload = _payload(Path("/repo"))
    del payload[missing]

    with pytest.raises(ValueError):
        _parse(payload)


def test_spec_rejects_unknown_and_duplicate_keys(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    payload = _payload(repository_root)

    with pytest.raises(ValueError):
        _parse({**payload, "extra": "not allowed"})

    raw = _canonical(payload)
    duplicate = raw.replace(
        b'"assignee":"codex-direct"',
        b'"assignee":"codex-direct","assignee":"codex-direct"',
        1,
    )
    with pytest.raises(ValueError):
        ApprovedExecutionSpec.from_canonical_bytes(duplicate)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "kanban-approved-execution-spec/v2"),
        ("backend", "hermes/v1"),
        ("board", "default"),
        ("assignee", "codex"),
        ("max_runtime_seconds", 1799),
        ("max_runtime_seconds", 1801),
        ("max_runtime_seconds", True),
    ],
)
def test_spec_rejects_wrong_fixed_contract_values(tmp_path, field, value):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()

    with pytest.raises(ValueError):
        _parse(_payload(repository_root, **{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "1234abcd"),
        ("task_id", "t_1234ABCd"),
        ("task_id", "t_1234abcde"),
        ("title", ""),
        ("instructions", ""),
        ("project_id", ""),
        ("branch_name", ""),
    ],
)
def test_spec_rejects_invalid_identity_or_empty_text(tmp_path, field, value):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    payload = _payload(repository_root, **{field: value})
    if field == "task_id":
        payload["worktree_path"] = str(repository_root / ".worktrees" / value)

    with pytest.raises(ValueError):
        _parse(payload)


def test_spec_rejects_non_nfc_strings(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    decomposed = "e\u0301"

    with pytest.raises(ValueError):
        _parse(_payload(repository_root, title=decomposed))


def test_spec_rejects_lone_surrogate_on_direct_construction(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="^invalid approved execution spec$"):
        ApprovedExecutionSpec(**_payload(repository_root, title="\ud800"))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: b" " + raw,
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b'"assignee":', b'"assignee": '),
        lambda raw: raw.replace("기록".encode(), b"\\uae30\\ub85d", 1),
    ],
)
def test_spec_rejects_mutated_noncanonical_bytes(tmp_path, mutation):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    raw = _canonical(_payload(repository_root))

    with pytest.raises(ValueError):
        ApprovedExecutionSpec.from_canonical_bytes(mutation(raw))


def test_spec_rejects_non_bytes_and_bytes_subclass(tmp_path):
    class BytesSubclass(bytes):
        pass

    raw = _canonical(_payload(tmp_path / "repo"))
    with pytest.raises(ValueError):
        ApprovedExecutionSpec.from_canonical_bytes(raw.decode("utf-8"))
    with pytest.raises(ValueError):
        ApprovedExecutionSpec.from_canonical_bytes(BytesSubclass(raw))


def test_spec_normalizes_excessive_json_nesting_to_closed_error(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    raw = _canonical(_payload(repository_root))
    nested = b"[" * 2000 + b"0" + b"]" * 2000
    hostile = raw.replace(
        '"instructions":"구현 지시"'.encode("utf-8"),
        b'"instructions":' + nested,
        1,
    )

    with pytest.raises(ValueError, match="^invalid approved execution spec$"):
        ApprovedExecutionSpec.from_canonical_bytes(hostile)


def test_spec_rejects_primitive_subclasses_on_direct_construction(tmp_path):
    class StringSubclass(str):
        pass

    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    payload = _payload(repository_root)
    payload["title"] = StringSubclass(payload["title"])

    with pytest.raises(ValueError):
        ApprovedExecutionSpec(**payload)


def test_spec_rejects_relative_noncanonical_and_outside_worktree_paths(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()

    invalid_pairs = [
        ("relative/repo", "relative/repo/.worktrees/t_1234abcd"),
        (str(repository_root / ".." / "repo"), str(repository_root / ".worktrees" / "t_1234abcd")),
        (str(repository_root), str(tmp_path / "outside" / "t_1234abcd")),
        (str(repository_root), str(repository_root / ".worktrees" / "other")),
    ]
    for root, worktree in invalid_pairs:
        with pytest.raises(ValueError):
            _parse(_payload(repository_root, repository_root=root, worktree_path=worktree))


def test_spec_normalizes_embedded_nul_path_to_closed_error(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="^invalid approved execution spec$"):
        ApprovedExecutionSpec(
            **_payload(repository_root, repository_root=str(repository_root) + "\x00")
        )


@pytest.mark.parametrize("field", ["repository_root", "worktree_path"])
@pytest.mark.parametrize("separator_text", ["trailing", "repeated"])
def test_spec_rejects_noncanonical_path_separator_text(tmp_path, field, separator_text):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    canonical = {
        "repository_root": str(repository_root),
        "worktree_path": str(repository_root / ".worktrees" / "t_1234abcd"),
    }[field]
    if separator_text == "trailing":
        noncanonical = canonical + "/"
    else:
        path = Path(canonical)
        noncanonical = f"{path.parent}//{path.name}"

    with pytest.raises(ValueError, match="^invalid approved execution spec$"):
        _parse(_payload(repository_root, **{field: noncanonical}))


def test_spec_rejects_symlink_repository_or_worktree_target(tmp_path):
    real_repository = tmp_path / "real-repo"
    real_repository.mkdir()
    repository_link = tmp_path / "repo-link"
    repository_link.symlink_to(real_repository, target_is_directory=True)

    with pytest.raises(ValueError):
        _parse(_payload(repository_link))

    worktrees = real_repository / ".worktrees"
    worktrees.mkdir()
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    target_link = worktrees / "t_1234abcd"
    target_link.symlink_to(real_target, target_is_directory=True)
    with pytest.raises(ValueError):
        _parse(_payload(real_repository))


def test_all_dtos_are_frozen_and_validate_closed_states(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    spec = _parse(_payload(repository_root))
    handle = ExecutionHandle(
        backend=BACKEND,
        pid=101,
        pgid=101,
        kernel_start_time=987654321,
    )
    running = ExecutionObservation(state=ExecutionState.RUNNING, exit_code=None)
    exited = ExecutionObservation(state=ExecutionState.EXITED, exit_code=0)
    mismatch = ExecutionObservation(
        state=ExecutionState.IDENTITY_MISMATCH,
        exit_code=None,
    )
    dead = TerminationObservation(state=TerminationState.DEAD)

    for dto in (spec, handle, running, exited, mismatch, dead):
        assert is_dataclass(dto)
        with pytest.raises(FrozenInstanceError):
            setattr(dto, next(iter(dto.__dataclass_fields__)), "mutated")

    with pytest.raises(ValueError):
        ExecutionObservation(state=ExecutionState.RUNNING, exit_code=0)
    with pytest.raises(ValueError):
        ExecutionObservation(state=ExecutionState.EXITED, exit_code=None)
    with pytest.raises(ValueError):
        ExecutionObservation(state="running", exit_code=None)
    with pytest.raises(ValueError):
        TerminationObservation(state="dead")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"backend": "other/v1", "pid": 1, "pgid": 1, "kernel_start_time": 1},
        {"backend": BACKEND, "pid": True, "pgid": 1, "kernel_start_time": 1},
        {"backend": BACKEND, "pid": 0, "pgid": 1, "kernel_start_time": 1},
        {"backend": BACKEND, "pid": 1, "pgid": 0, "kernel_start_time": 1},
        {"backend": BACKEND, "pid": 1, "pgid": 1, "kernel_start_time": 0},
    ],
)
def test_execution_handle_rejects_open_or_invalid_identity(kwargs):
    with pytest.raises(ValueError):
        ExecutionHandle(**kwargs)


def test_execution_backend_protocol_has_only_the_closed_port_methods():
    methods = {
        name
        for name, value in ExecutionBackend.__dict__.items()
        if inspect.isfunction(value) and not name.startswith("_")
    }
    assert methods == {"prepare", "release", "observe", "terminate"}
    assert ExecutionBackend.__annotations__ == {"kind": str}


class _FakeBackend:
    kind = BACKEND

    def prepare(self, spec, worktree):
        return ExecutionHandle(self.kind, 10, 10, 100)

    def release(self, handle):
        return None

    def observe(self, handle):
        return ExecutionObservation(ExecutionState.RUNNING, None)

    def terminate(self, handle):
        return TerminationObservation(TerminationState.DEAD)


def test_fake_backend_can_implement_port_without_infrastructure(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    spec = _parse(_payload(repository_root))
    backend: ExecutionBackend = _FakeBackend()

    handle = backend.prepare(spec, Path(spec.worktree_path))
    backend.release(handle)
    assert backend.observe(handle).state is ExecutionState.RUNNING
    assert backend.terminate(handle).state is TerminationState.DEAD


def test_domain_module_has_no_infrastructure_imports():
    source_path = Path(__file__).parents[2] / "hermes_cli" / "kanban_execution.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        "codex",
        "subprocess",
        "sqlite3",
        "gateway",
        "tools.process_registry",
        "hermes_cli.kanban_db",
    }
    assert not {
        name
        for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    }
