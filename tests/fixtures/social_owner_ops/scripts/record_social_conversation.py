from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _expected(document: dict[str, Any]) -> dict[str, Any]:
    source = document["sources"][0]
    identity = {
        "schema": "social-conversation-identity/v1",
        "person_id": document["person_id"],
        "sources": document["sources"],
    }
    event_id = "social_v1_" + hashlib.sha256(
        _canonical(identity).encode("utf-8")
    ).hexdigest()[:16]
    payload_hash = hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()
    source_digest = hashlib.sha256(
        f"source|{event_id}|{_canonical(source)}".encode("utf-8")
    ).hexdigest()
    return {
        "event_id": event_id,
        "payload_hash": payload_hash,
        "source_id": f"lifelog-{source_digest[:24]}",
        "summary": (
            f"대화 상대: {document['partner_label']}. 확인된 본인 발언·결정·소회: "
            + " / ".join(document["confirmed_points"])
        ),
    }


def apply_payload(
    db: Path,
    document: dict[str, Any],
    dry_run: bool,
    *,
    pre_live_check: Callable[[], object] | None = None,
) -> dict[str, Any]:
    expected = _expected(document)
    if dry_run:
        inserted = 0
    else:
        if pre_live_check is None:
            raise PermissionError("pre-live check required")
        pre_live_check()
        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_hash FROM life_events WHERE id=?",
                (expected["event_id"],),
            ).fetchone()
            if existing is not None:
                if existing != (expected["payload_hash"],):
                    raise RuntimeError("fixture identity conflict")
                inserted = 0
            else:
                created_at = (
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                source = document["sources"][0]
                connection.execute(
                    "INSERT INTO life_events("
                    "id,occurred_at,ended_at,timezone,event_type,title,summary,"
                    "source_type,source_ref,confidence,raw_text_hash,payload_hash,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        expected["event_id"],
                        document["occurred_at"],
                        document.get("ended_at"),
                        document["timezone"],
                        "note",
                        "중요한 대화 기록",
                        expected["summary"],
                        "discord",
                        f"discord:{source['channel_id']}:{source['thread_id']}:{source['message_id']}",
                        1.0,
                        None,
                        expected["payload_hash"],
                        created_at,
                        created_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO life_event_people(event_id,person_id,role,confidence) "
                    "VALUES (?,?,?,?)",
                    (expected["event_id"], document["person_id"], "subject", 1.0),
                )
                connection.executemany(
                    "INSERT INTO life_event_tags(event_id,tag) VALUES (?,?)",
                    [(expected["event_id"], tag) for tag in document["tags"]],
                )
                connection.execute(
                    "INSERT INTO life_sources("
                    "id,event_id,platform,guild_id,channel_id,thread_id,message_id,"
                    "path,redacted_excerpt,captured_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        expected["source_id"],
                        expected["event_id"],
                        "discord",
                        source["guild_id"],
                        source["channel_id"],
                        source["thread_id"],
                        source["message_id"],
                        None,
                        None,
                        created_at,
                    ),
                )
                inserted = 1
            connection.commit()
    return {
        "schema": "social-conversation-record-result/v1",
        "event_ids": [expected["event_id"]],
        "payload_hash": expected["payload_hash"],
        "inserted_events": inserted,
        "dry_run": dry_run,
    }
