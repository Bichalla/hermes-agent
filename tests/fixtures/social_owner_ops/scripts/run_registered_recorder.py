from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

_SOCIAL_PINNED_VALIDATOR: tuple[Path, bytes] | None = None


def _load_social_recorder(_root: Path) -> ModuleType:
    raise RuntimeError("core must inject the pinned recorder")


def run_registered_social_document(
    document_bytes: bytes,
    pre_live_check: Callable[[], object],
    root: Path,
) -> dict[str, Any]:
    if type(document_bytes) is not bytes or not callable(pre_live_check):
        raise ValueError("invalid synthetic invocation")
    document = json.loads(document_bytes.decode("utf-8"))
    recorder = _load_social_recorder(root)
    expected = recorder._expected(document)
    dry = recorder.apply_payload(root / "lifelog.db", document, True)
    if dry["event_ids"] != [expected["event_id"]]:
        raise RuntimeError("synthetic dry-run mismatch")
    pre_live_check()
    live = recorder.apply_payload(
        root / "lifelog.db",
        document,
        False,
        pre_live_check=pre_live_check,
    )
    replay = recorder.apply_payload(
        root / "lifelog.db",
        document,
        False,
        pre_live_check=pre_live_check,
    )
    if replay["inserted_events"] != 0:
        raise RuntimeError("synthetic replay failed")
    return {
        "schema": "registered-recorder-result/v1",
        "recorder_id": "social_conversation.v1",
        "exit_status": 0,
        "validation_status": "validator_and_exact_readback_passed",
        "idempotency_result": (
            "inserted" if live["inserted_events"] == 1 else "existing"
        ),
        "event_ids": [expected["event_id"]],
        "payload_hash": expected["payload_hash"],
        "dry_run": False,
        "backup_status": "verified",
        "replay_status": "verified",
        "readback": "passed",
    }
