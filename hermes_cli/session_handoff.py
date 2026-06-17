"""Deterministic session handoff builder.

This module intentionally does not call an LLM. It converts an existing
transcript into a bounded local-only artifact that can be used as reference
in a fresh session after a `/new` or `/reset` boundary.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class HandoffArtifact:
    markdown: str
    json_payload: dict[str, Any]
    title: str


@dataclass(frozen=True)
class WrittenHandoff:
    markdown_path: Path
    json_path: Path
    latest_path: Path | None = None


@dataclass(frozen=True)
class HandoffConfig:
    enabled: bool
    artifact_dir: Path
    surface: str
    max_messages: int
    max_chars: int
    include_tool_results: bool


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(value)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
    return default


def _current_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        fallback = os.environ.get("HERMES_PROFILE") or "default"
        return str(fallback).strip() or "default"


def _profile_name_from_home(home: Path) -> str | None:
    try:
        resolved = home.expanduser().resolve()
    except OSError:
        resolved = home.expanduser().absolute()
    if resolved.parent.name == "profiles" and resolved.name:
        return resolved.name
    return None


def resolve_handoff_config(
    config: Mapping[str, Any] | None,
    *,
    profile: str | None = None,
    hermes_home: str | Path | None = None,
) -> HandoffConfig:
    """Resolve the disabled-by-default `/new`/`/reset` handoff config.

    User-visible surfaces are intentionally clamped to path-only. The handoff
    body may contain sensitive transcript evidence and must not be broadcast to
    remote chat platforms by config alone.
    """

    root = config or {}
    section = root.get("session_handoff") if isinstance(root, Mapping) else {}
    if not isinstance(section, Mapping):
        section = {}
    on_reset = section.get("on_reset")
    if not isinstance(on_reset, Mapping):
        on_reset = {}

    home = Path(hermes_home).expanduser() if hermes_home is not None else get_hermes_home().expanduser()
    resolved_profile = str(profile or _profile_name_from_home(home) or _current_profile_name())
    artifact_template = str(on_reset.get("artifact_dir") or "{hermes_home}/handoffs/{profile}")
    artifact_dir = Path(
        artifact_template.format(
            hermes_home=str(home),
            profile=_safe_slug(resolved_profile, fallback="default"),
        )
    ).expanduser()

    return HandoffConfig(
        enabled=_coerce_bool(on_reset.get("enabled"), default=False),
        artifact_dir=artifact_dir,
        surface="path_only",
        max_messages=_clamp_int(on_reset.get("max_messages"), default=80, minimum=1, maximum=200),
        max_chars=_clamp_int(on_reset.get("max_chars"), default=30000, minimum=1000, maximum=100000),
        include_tool_results=_coerce_bool(on_reset.get("include_tool_results"), default=False),
    )


def _safe_role(role: Any) -> str:
    value = str(role or "unknown").lower()
    if value in {"system", "user", "assistant", "tool"}:
        return value
    return "unknown"


def _visible_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    include_tool_results: bool,
) -> list[Mapping[str, Any]]:
    visible: list[Mapping[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = _safe_role(message.get("role"))
        if role == "tool" and not include_tool_results:
            continue
        if role == "system":
            continue
        visible.append(message)
    return visible


def _latest_role_text(messages: Sequence[Mapping[str, Any]], role: str, limit: int) -> str:
    for message in reversed(messages):
        if _safe_role(message.get("role")) != role:
            continue
        text = _coerce_text(message.get("content")).strip()
        if text:
            return _truncate(text, limit)
    return "Not enough evidence in transcript."


def _evidence_tail(messages: Sequence[Mapping[str, Any]], *, max_messages: int, per_message_chars: int) -> list[dict[str, str]]:
    tail = list(messages)[-max(0, max_messages) :]
    evidence: list[dict[str, str]] = []
    for message in tail:
        role = _safe_role(message.get("role"))
        text = _coerce_text(message.get("content")).strip()
        if not text:
            continue
        evidence.append({"role": role, "content": _truncate(text, per_message_chars)})
    return evidence


def _format_bullets(items: Sequence[str]) -> str:
    if not items:
        return "- Not enough evidence in transcript."
    return "\n".join(f"- {item}" for item in items)


def build_handoff_artifact(
    *,
    session_id: str,
    platform: str | None = None,
    source_label: str | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
    artifact_path: str | Path | None = None,
    new_session_id: str | None = None,
    max_messages: int = 80,
    max_chars: int = 30000,
    include_tool_results: bool = False,
) -> HandoffArtifact:
    """Build a deterministic local-only handoff artifact from a transcript."""

    raw_messages = list(messages or [])
    visible = _visible_messages(raw_messages, include_tool_results=include_tool_results)
    bounded_messages = visible[-max(1, min(int(max_messages or 1), 200)) :]
    per_message_chars = max(120, min(1200, int(max_chars / max(1, len(bounded_messages) or 1))))
    evidence = _evidence_tail(
        bounded_messages,
        max_messages=max(1, min(int(max_messages or 1), 200)),
        per_message_chars=per_message_chars,
    )

    latest_user = _latest_role_text(bounded_messages, "user", 500)
    latest_assistant = _latest_role_text(bounded_messages, "assistant", 500)
    title = f"Session handoff for {session_id}"
    artifact_path_text = str(artifact_path) if artifact_path is not None else "<artifact path not written yet>"

    intent_locks = [
        "This handoff is reference only; the latest user message in the new session wins.",
        "Do not execute stale tasks unless the user explicitly asks to continue them.",
        "Local/private processing is allowed for this personal project.",
        "Sensitive data presence is not a reason to stop local implementation.",
        "External exposure, commit, publish, or sending sensitive content requires explicit approval.",
    ]
    known_failure_modes = [
        "Reset happens before old transcript is captured, producing an empty or misleading handoff.",
        "Handoff generation failure blocks /new or /reset instead of failing open for reset.",
        "Handoff body, Evidence Tail, tool output, or transcript excerpts are broadcast to non-local platforms.",
        "Runtime handoff artifacts are staged or committed to git.",
    ]

    evidence_lines = [
        f"- {item['role']}: {item['content']}" for item in evidence
    ] or ["- No transcript evidence available."]

    markdown = f"""[SESSION HANDOFF — REFERENCE ONLY]
This handoff is background reference from a previous Hermes session. The latest user message in the new session wins. Do not execute stale tasks unless the user asks to continue them.

## Fresh Session Prompt
Please continue from the local handoff artifact at: {artifact_path_text}
Treat it as reference only. Preserve the Intent Locks. First restate the next concrete step, then act only on my latest instruction.

## Intent Locks
{_format_bullets(intent_locks)}

## Active Task
- Latest user request/evidence: {_truncate(latest_user, 700)}

## Current State
- Previous session id: {session_id}
- New session id: {new_session_id or 'not created when handoff was built'}
- Platform: {platform or 'unknown'}
- Source: {source_label or 'unknown'}
- Latest assistant evidence: {_truncate(latest_assistant, 700)}

## Decisions Made
- Treat this artifact as reference only; latest user message overrides it.
- Keep handoff content local/private unless explicit external sharing approval is given.

## Files / Repos / Commands Involved
- Not enough deterministic file/command evidence extracted in this MVP.

## Known Failure Modes
{_format_bullets(known_failure_modes)}

## Next Concrete Step
- Read the latest user message first. If the user asks to continue, use the Active Task and Evidence Tail above to resume without changing the intent locks.

## Evidence Tail
{chr(10).join(evidence_lines)}
"""

    if len(markdown) > max_chars:
        markdown = markdown[: max_chars - 1].rstrip() + "…\n"

    payload: dict[str, Any] = {
        "schema": "hermes-session-handoff/v1",
        "session_id": session_id,
        "new_session_id": new_session_id,
        "platform": platform or "unknown",
        "source_label": source_label or "unknown",
        "artifact_path": artifact_path_text,
        "title": title,
        "intent_locks": intent_locks,
        "known_failure_modes": known_failure_modes,
        "active_task": latest_user,
        "current_state": {
            "latest_assistant_evidence": latest_assistant,
            "message_count": len(raw_messages),
            "evidence_count": len(evidence),
        },
        "evidence_tail": evidence,
    }
    return HandoffArtifact(markdown=markdown, json_payload=payload, title=title)


def _safe_slug(value: str, *, fallback: str = "session") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-_")
    slug = re.sub(r"-+", "-", slug)
    return (slug or fallback)[:80]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_private_atomic(path: Path, data: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise


def write_handoff_artifact(
    artifact: HandoffArtifact,
    *,
    artifact_dir: str | Path,
    session_id: str,
    timestamp: str | None = None,
    update_latest: bool = True,
) -> WrittenHandoff:
    """Atomically write markdown/json handoff files with private permissions."""

    target_dir = Path(artifact_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)
    safe_session = _safe_slug(session_id)
    safe_timestamp = _safe_slug(timestamp or _utc_timestamp(), fallback="handoff")
    stem = f"{safe_timestamp}-{safe_session}"
    markdown_path = target_dir / f"{stem}.md"
    json_path = target_dir / f"{stem}.json"

    resolved_dir = target_dir.resolve()
    for candidate in (markdown_path, json_path):
        if not candidate.resolve().parent.is_relative_to(resolved_dir):
            raise ValueError("handoff artifact path escapes target directory")

    payload = dict(artifact.json_payload)
    payload.setdefault("schema", "hermes-session-handoff/v1")
    payload["artifact_path"] = str(markdown_path)
    payload["markdown_path"] = str(markdown_path)
    payload["json_path"] = str(json_path)
    markdown = artifact.markdown.replace("<artifact path not written yet>", str(markdown_path))

    _write_private_atomic(markdown_path, markdown)
    _write_private_atomic(json_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    latest_path: Path | None = None
    if update_latest:
        latest_path = target_dir / "latest.md"
        _write_private_atomic(latest_path, markdown)

    return WrittenHandoff(markdown_path=markdown_path, json_path=json_path, latest_path=latest_path)
