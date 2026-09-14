"""Reversible file removal for delegated Kanban workspaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid


class SoftDeleteError(ValueError):
    """Raised when a requested soft delete or restore is outside the safe contract."""


_RECEIPT_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_PATH_CHARS = 4096


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    workspace: str
    original_relpath: str
    trash_payload: str
    created_at: float
    status: str
    kind: str
    dev: int
    ino: int


def soft_delete(workspace: Path, target: str, trash_root: Path) -> dict:
    """Move a regular file or plain directory tree into private trash.

    This uses ``os.rename`` only.  Cross-device moves fail with ``EXDEV`` instead
    of falling back to copy-then-delete.
    """

    ws = _workspace(workspace)
    trash = _prepare_trash_root(trash_root)
    target_path = _target_path(ws, target)
    _refuse_trash_overlap(target_path, trash)
    st = _validate_target_tree(ws, target_path)
    relpath = target_path.relative_to(ws).as_posix()
    receipt_id = uuid.uuid4().hex
    receipt_dir = _receipt_dir(trash, receipt_id)
    payload = receipt_dir / "payload"
    meta_path = receipt_dir / "receipt.json"
    receipt_dir.mkdir(mode=0o700, parents=True)
    receipt = Receipt(
        receipt_id=receipt_id,
        workspace=str(ws),
        original_relpath=relpath,
        trash_payload=str(payload),
        created_at=time.time(),
        status="prepared",
        kind="dir" if stat.S_ISDIR(st.st_mode) else "file",
        dev=st.st_dev,
        ino=st.st_ino,
    )
    _write_private_json(meta_path, asdict(receipt))
    try:
        os.rename(target_path, payload)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise SoftDeleteError("soft delete refused cross-device move") from None
        raise
    moved = {**asdict(receipt), "status": "moved"}
    _write_private_json(meta_path, moved)
    return moved


def restore(workspace: Path, receipt_id: str, trash_root: Path) -> dict:
    """Restore a soft-deleted path if the original location is still free."""

    _validate_receipt_id(receipt_id)
    ws = _workspace(workspace)
    trash = _prepare_trash_root(trash_root)
    receipt_dir = _receipt_dir(trash, receipt_id)
    meta_path = receipt_dir / "receipt.json"
    data = _read_receipt(meta_path)
    if data["status"] not in {"moved", "prepared"}:
        raise SoftDeleteError("receipt is not restorable")
    if data["workspace"] != str(ws):
        raise SoftDeleteError("receipt belongs to a different workspace")
    target = _target_path(ws, data["original_relpath"])
    if target.exists() or target.is_symlink():
        raise SoftDeleteError("restore target already exists")
    payload = receipt_dir / "payload"
    _refuse_trash_overlap(target, trash)
    _validate_payload(payload, data)
    _rename_no_replace(payload, target)
    restored = {**data, "status": "restored", "restored_at": time.time()}
    _write_private_json(meta_path, restored)
    return restored


def _workspace(workspace: Path) -> Path:
    ws = Path(workspace).expanduser().resolve(strict=True)
    st = ws.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise SoftDeleteError("workspace must be a real directory")
    return ws


def _prepare_trash_root(trash_root: Path) -> Path:
    trash = Path(trash_root).expanduser()
    if not trash.is_absolute():
        raise SoftDeleteError("trash_root must be absolute")
    if trash.exists():
        st = trash.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise SoftDeleteError("trash_root must be a real directory")
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise SoftDeleteError("trash_root must be private")
    else:
        trash.mkdir(mode=0o700, parents=True)
    os.chmod(trash, 0o700)
    return trash.resolve(strict=True)


def _target_path(workspace: Path, target: str) -> Path:
    if not isinstance(target, str) or not target.strip() or len(target) > _MAX_PATH_CHARS:
        raise SoftDeleteError("invalid target")
    raw = Path(target)
    if raw.is_absolute() or any(part in ("", ".", "..") for part in raw.parts):
        raise SoftDeleteError("target must be a normal relative path")
    _refuse_symlink_ancestors(workspace, raw)
    candidate = workspace.joinpath(raw)
    resolved_parent = candidate.parent.resolve(strict=True)
    if not _is_relative_to(resolved_parent, workspace):
        raise SoftDeleteError("target escapes workspace")
    if candidate.name == ".git" or ".git" in candidate.relative_to(workspace).parts:
        raise SoftDeleteError("refusing to soft-delete .git paths")
    return candidate


def _refuse_symlink_ancestors(workspace: Path, raw: Path) -> None:
    current = workspace
    for part in raw.parts[:-1]:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            raise SoftDeleteError("target parent does not exist") from None
        if stat.S_ISLNK(st.st_mode):
            raise SoftDeleteError("refusing target with symlink ancestor")
        if not stat.S_ISDIR(st.st_mode):
            raise SoftDeleteError("target parent is not a directory")


def _validate_target_tree(workspace: Path, target: Path) -> os.stat_result:
    if target == workspace:
        raise SoftDeleteError("refusing to soft-delete workspace root")
    try:
        st = target.lstat()
    except FileNotFoundError:
        raise SoftDeleteError("target does not exist") from None
    if stat.S_ISLNK(st.st_mode):
        raise SoftDeleteError("refusing symlink target")
    if stat.S_ISREG(st.st_mode):
        return st
    if not stat.S_ISDIR(st.st_mode):
        raise SoftDeleteError("refusing special file")
    for root, dirs, files in os.walk(target, topdown=True, followlinks=False):
        root_path = Path(root)
        _validate_child_path(workspace, root_path)
        for name in list(dirs):
            child = root_path / name
            child_st = child.lstat()
            if stat.S_ISLNK(child_st.st_mode):
                raise SoftDeleteError("refusing directory containing symlink")
            if not stat.S_ISDIR(child_st.st_mode):
                raise SoftDeleteError("refusing directory containing special entry")
        for name in files:
            child = root_path / name
            child_st = child.lstat()
            if stat.S_ISLNK(child_st.st_mode) or not stat.S_ISREG(child_st.st_mode):
                raise SoftDeleteError("refusing directory containing non-regular file")
            _validate_child_path(workspace, child)
    return st


def _validate_child_path(workspace: Path, child: Path) -> None:
    resolved = child.parent.resolve(strict=True) / child.name
    try:
        rel = resolved.relative_to(workspace)
    except ValueError:
        raise SoftDeleteError("target escapes workspace") from None
    if ".git" in rel.parts:
        raise SoftDeleteError("refusing to soft-delete .git paths")


def _validate_payload(payload: Path, receipt: dict) -> None:
    try:
        st = payload.lstat()
    except FileNotFoundError:
        raise SoftDeleteError("trash payload is missing") from None
    if stat.S_ISLNK(st.st_mode):
        raise SoftDeleteError("trash payload is a symlink")
    expected_dir = receipt["kind"] == "dir"
    if expected_dir != stat.S_ISDIR(st.st_mode):
        raise SoftDeleteError("trash payload type changed")
    if int(receipt.get("dev", -1)) != st.st_dev or int(receipt.get("ino", -1)) != st.st_ino:
        raise SoftDeleteError("trash payload identity changed")


def _rename_no_replace(src: Path, dst: Path) -> None:
    if hasattr(os, "renameat2"):
        try:
            os.renameat2(os.AT_FDCWD, src, os.AT_FDCWD, dst, os.RENAME_NOREPLACE)  # type: ignore[attr-defined]
            return
        except FileExistsError:
            raise SoftDeleteError("restore target already exists") from None
        except AttributeError:
            pass
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise SoftDeleteError("restore target already exists") from None
            raise
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, "renamex_np"):
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        RENAME_EXCL = 0x00000004
        result = renamex_np(os.fsencode(src), os.fsencode(dst), RENAME_EXCL)
        if result == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise SoftDeleteError("restore target already exists") from None
        raise OSError(err, os.strerror(err), str(dst))
    raise SoftDeleteError("atomic no-overwrite restore is unsupported on this platform")


def _refuse_trash_overlap(target: Path, trash_root: Path) -> None:
    try:
        target_resolved = target.resolve(strict=False)
    except OSError:
        target_resolved = target
    if (
        _is_relative_to(target_resolved, trash_root)
        or target_resolved == trash_root
        or _is_relative_to(trash_root, target_resolved)
    ):
        raise SoftDeleteError("target overlaps trash root")


def _receipt_dir(trash_root: Path, receipt_id: str) -> Path:
    _validate_receipt_id(receipt_id)
    return trash_root / "receipts" / receipt_id


def _validate_receipt_id(receipt_id: str) -> None:
    if not isinstance(receipt_id, str) or not _RECEIPT_RE.fullmatch(receipt_id):
        raise SoftDeleteError("invalid receipt id")


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
            os.chmod(path, 0o600)
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_receipt(path: Path) -> dict:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SoftDeleteError("receipt must be private")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"receipt_id", "workspace", "original_relpath", "trash_payload", "status", "kind"}
    if not isinstance(data, dict) or not required <= set(data):
        raise SoftDeleteError("invalid receipt")
    _validate_receipt_id(data["receipt_id"])
    return data


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
