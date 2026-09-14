"""Recoverable deletion tools scoped to the worker's current database lease."""
import json
import os
from pathlib import Path

from .broker import _ro_connect, validate_current_request
from .protocol import ApprovalBridgeRequest
from .soft_delete import soft_delete, restore


def _workspace(identity, config) -> Path:
    if identity is None:
        raise ValueError("active Kanban worker required")
    request = ApprovalBridgeRequest.create(
        command="kanban_soft_delete", description="Recoverable file operation",
        pattern_key="soft-delete", pattern_keys=["soft-delete"], session_key="soft-delete-tool",
        task_id=identity.task_id, run_id=int(identity.run_id), claim_lock=identity.claim_lock,
        worker_pid=identity.worker_pid, db_path=os.path.realpath(identity.db_path), profile=identity.profile,
        timeout_seconds=30,
    )
    validate_current_request(config, request)
    db = _ro_connect(config.db_path)
    try:
        row = db.execute("SELECT workspace_path FROM tasks WHERE id=?", (identity.task_id,)).fetchone()
        if row is None or not row[0] or not Path(row[0]).is_absolute():
            raise ValueError("active task has no local workspace")
        return Path(row[0])
    finally:
        db.close()


def register_tools(ctx, identity, config, root: Path) -> None:
    def remove(args, **_kw):
        try:
            workspace = _workspace(identity, config)
            return json.dumps(soft_delete(workspace, args["path"], root / ".local/trash"))
        except Exception:
            return json.dumps({"error": "Soft-delete refused: verify the live task workspace and use a regular file/directory on the same filesystem. Nothing is purged."})

    def recover(args, **_kw):
        try:
            workspace = _workspace(identity, config)
            return json.dumps(restore(workspace, args["receipt_id"], root / ".local/trash"))
        except Exception:
            return json.dumps({"error": "Restore refused: verify the receipt, live workspace and vacant original location. Preserved data is not purged."})

    for name, field, handler, description in [
        ("kanban_soft_delete", "path", remove, "Recoverably remove a workspace file/directory by moving it into private trash. No human approval; returns a restore receipt. Never purges. Relative workspace path only."),
        ("kanban_restore", "receipt_id", recover, "Restore a prior kanban_soft_delete receipt into its original workspace location without overwriting anything."),
    ]:
        ctx.register_tool(name=name, toolset="file", handler=handler, description=description,
                          schema={"name": name, "description": description, "parameters": {
                              "type": "object", "properties": {field: {"type": "string"}},
                              "required": [field], "additionalProperties": False,
                          }})

    def no_patch_delete(tool_name="", args=None, **_kw):
        if tool_name not in {"patch", "apply_patch"} or not isinstance(args, dict):
            return None
        changes = args.get("changes", [])
        deletes = any(
            isinstance(change, dict) and (
                change.get("kind") == "delete" or
                isinstance(change.get("kind"), dict) and change["kind"].get("type") == "delete"
            ) for change in changes
        ) if isinstance(changes, list) else False
        if "delete file:" in str(args.get("patch", "")).lower() or deletes:
            return {"action": "block", "message": "Use kanban_soft_delete for recoverable file deletion. Permanent deletion requires the owner's explicit approval via the command policy."}
        return None

    ctx.register_hook("pre_tool_call", no_patch_delete)
    ctx.register_system_prompt_section(
        "kanban-deletion-policy",
        "The owner delegates normal development approvals to work-pm. Do not ask for routine Allow Once clicks. "
        "Use kanban_soft_delete for file removal and kanban_restore to undo it; keep the restore receipt. "
        "Hard-delete always requires explicit human permission. A backup claim or a soft-delete label does not authorize irreversible deletion. "
        "Opaque commands denied by policy must be decomposed into inspectable operations, never disguised or encoded.",
    )
