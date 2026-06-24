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
    preview_enabled: bool = False
    preview_max_items: int = 4
    preview_max_chars: int = 600


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

    preview = on_reset.get("preview")
    if not isinstance(preview, Mapping):
        preview = {}

    return HandoffConfig(
        enabled=_coerce_bool(on_reset.get("enabled"), default=False),
        artifact_dir=artifact_dir,
        surface="path_only",
        max_messages=_clamp_int(on_reset.get("max_messages"), default=80, minimum=1, maximum=200),
        max_chars=_clamp_int(on_reset.get("max_chars"), default=30000, minimum=1000, maximum=100000),
        include_tool_results=_coerce_bool(on_reset.get("include_tool_results"), default=False),
        preview_enabled=_coerce_bool(preview.get("enabled"), default=False),
        preview_max_items=_clamp_int(preview.get("max_items"), default=4, minimum=1, maximum=6),
        preview_max_chars=_clamp_int(preview.get("max_chars"), default=600, minimum=160, maximum=1200),
    )


def _safe_role(role: Any) -> str:
    value = str(role or "unknown").lower()
    if value in {"system", "user", "assistant", "tool"}:
        return value
    return "unknown"


_META_PREFIXES = (
    "[CONTEXT COMPACTION — REFERENCE ONLY]",
    "[CONTEXT COMPACTION - REFERENCE ONLY]",
    "[SESSION HANDOFF — REFERENCE ONLY]",
    "[SESSION HANDOFF - REFERENCE ONLY]",
    "[Your active task list was preserved across context compression]",
    "[Your active task list was preserved across context compaction]",
)

_COMPLETION_MARKERS = (
    "record complete",
    "기록 완료",
    "검증 완료",
    "validation ok",
    "event_id",
    "passed",
    "commit",
    "pushed",
    "updated",
    "완료",
)

_NEXT_MARKERS = (
    "next",
    "다음",
    "저녁",
    "follow-up",
    "이어",
    "if the user",
    "사용자가",
    "tonight",
)

_SENSITIVE_HINTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "api-key",
    "apikey",
    "access_key",
    "access-key",
    "accesskey",
    "bearer",
    "authorization",
)
_COMMAND_PREFIXES = (
    "scripts/run_tests.sh",
    "python ",
    "python3 ",
    "pytest ",
    "hermes ",
    "git ",
    "npm ",
    "uv ",
)
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home|tmp|var|opt|srv)/[^\s`)'\",]+")
_CODE_SPAN_RE = re.compile(r"`([^`]{1,400})`")
_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_CONFIG_KEY_RE = re.compile(r"\b[a-zA-Z_][\w-]*(?:\.[a-zA-Z_][\w-]*){2,}(?:=(?:true|false|[\w./:-]+))?\b")


def _is_handoff_meta_text(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in _META_PREFIXES)


def _split_sentences(text: str) -> list[str]:
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    for raw_line in raw.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)(?:\[[ xX>!-]\]\s*)?", "", line).strip()
        if not line:
            continue
        for piece in re.split(r"(?<=[.!?。])\s+", line):
            compact = " ".join(piece.split()).strip()
            if compact:
                pieces.append(compact)
    return pieces


def _has_marker(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def _safe_inventory_item(value: str, *, limit: int = 240) -> str | None:
    compact = " ".join(str(value).split()).strip().rstrip(".,;:")
    if not compact:
        return None
    lowered = compact.lower()
    if any(hint in lowered for hint in _SENSITIVE_HINTS):
        return None
    return _truncate(compact, limit)


def _extract_evidence_inventory(messages: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    inventory: dict[str, list[str]] = {
        "files": [],
        "repos": [],
        "commands": [],
        "config_keys": [],
        "commits": [],
    }
    seen: dict[str, set[str]] = {key: set() for key in inventory}

    def add(key: str, value: str) -> None:
        item = _safe_inventory_item(value)
        if item is None or item in seen[key] or len(inventory[key]) >= 12:
            return
        seen[key].add(item)
        inventory[key].append(item)

    for message in messages:
        text = _coerce_text(message.get("content"))
        for path_match in _ABS_PATH_RE.findall(text):
            add("files", path_match)
        for span in _CODE_SPAN_RE.findall(text):
            stripped = span.strip()
            if stripped.startswith(_COMMAND_PREFIXES):
                add("commands", stripped)
            if _CONFIG_KEY_RE.fullmatch(stripped):
                add("config_keys", stripped.split("=", 1)[0])
            if sha_match := _COMMIT_RE.match(stripped):
                add("commits", sha_match.group(0))
        for key_match in _CONFIG_KEY_RE.findall(text):
            add("config_keys", key_match.split("=", 1)[0])
        if "commit" in text.lower():
            for sha in _COMMIT_RE.findall(text):
                add("commits", sha)

    for path in list(inventory["files"]):
        marker = "/hermes-agent/"
        if marker in path:
            add("repos", path.split(marker, 1)[0] + "/hermes-agent")
    return inventory


def _format_inventory_section(inventory: Mapping[str, Sequence[str]]) -> str:
    labels = {
        "repos": "Repos",
        "files": "Files",
        "commands": "Commands",
        "config_keys": "Config keys",
        "commits": "Commits",
    }
    lines: list[str] = []
    for key, label in labels.items():
        values = list(inventory.get(key) or [])
        if values:
            rendered = ", ".join(f"`{value}`" for value in values[:12])
            lines.append(f"- {label}: {rendered}")
    return "\n".join(lines) if lines else "- Not enough deterministic file/command evidence extracted."


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
        text = _coerce_text(message.get("content")).strip()
        if role == "tool" and not include_tool_results:
            continue
        if role == "system":
            continue
        if text and _is_handoff_meta_text(text):
            continue
        visible.append(message)
    return visible


def _quality_counts(
    messages: Sequence[Mapping[str, Any]],
    *,
    include_tool_results: bool,
) -> dict[str, int]:
    raw_count = len(messages)
    tool_excluded = 0
    system_excluded = 0
    meta_filtered = 0
    invalid_filtered = 0
    for message in messages:
        if not isinstance(message, Mapping):
            invalid_filtered += 1
            continue
        role = _safe_role(message.get("role"))
        text = _coerce_text(message.get("content")).strip()
        if role == "tool" and not include_tool_results:
            tool_excluded += 1
        if role == "system":
            system_excluded += 1
        if text and _is_handoff_meta_text(text):
            meta_filtered += 1
    return {
        "raw_message_count": raw_count,
        "tool_messages_excluded": tool_excluded,
        "system_messages_excluded": system_excluded,
        "filtered_meta_messages": meta_filtered,
        "invalid_messages_filtered": invalid_filtered,
    }


def _latest_role_text(messages: Sequence[Mapping[str, Any]], role: str, limit: int) -> str:
    for message in reversed(messages):
        if _safe_role(message.get("role")) != role:
            continue
        text = _coerce_text(message.get("content")).strip()
        if text:
            return _truncate(text, limit)
    return "Not enough evidence in transcript."


def _latest_role_raw_text(messages: Sequence[Mapping[str, Any]], role: str) -> str:
    for message in reversed(messages):
        if _safe_role(message.get("role")) != role:
            continue
        text = _coerce_text(message.get("content")).strip()
        if text:
            return text
    return ""


def _evidence_tail(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_messages: int,
    per_message_chars: int,
) -> tuple[list[dict[str, str]], bool]:
    tail = list(messages)[-max(0, max_messages) :]
    evidence: list[dict[str, str]] = []
    truncated = False
    for message in tail:
        role = _safe_role(message.get("role"))
        text = _coerce_text(message.get("content")).strip()
        if not text:
            continue
        rendered = _truncate(text, per_message_chars)
        truncated = truncated or rendered != " ".join(text.split())
        evidence.append({"role": role, "content": rendered})
    return evidence, truncated


def _extract_last_completed_action(latest_assistant: str) -> str:
    if not latest_assistant or latest_assistant == "Not enough evidence in transcript.":
        return "Not enough evidence in transcript."
    if _has_marker(latest_assistant, _COMPLETION_MARKERS):
        return _truncate(latest_assistant, 900)
    return "Not enough deterministic completion evidence in transcript."


def _extract_completed_actions(latest_assistant: str, *, max_items: int = 5) -> list[str]:
    if not latest_assistant or latest_assistant == "Not enough evidence in transcript.":
        return []
    actions: list[str] = []
    for sentence in _split_sentences(latest_assistant):
        if _has_marker(sentence, _NEXT_MARKERS):
            continue
        if _has_marker(sentence, _COMPLETION_MARKERS):
            actions.append(_truncate(sentence, 240))
        if len(actions) >= max_items:
            break
    if actions:
        return actions
    if _has_marker(latest_assistant, _COMPLETION_MARKERS):
        return [_truncate(latest_assistant, 240)]
    return []


def _extract_open_loop(latest_user: str, latest_assistant: str) -> str:
    candidates: list[str] = []
    for sentence in _split_sentences(latest_assistant):
        if _has_marker(sentence, _NEXT_MARKERS):
            candidates.append(sentence)
    if candidates:
        return _truncate(" ".join(candidates), 700)
    return "Not enough evidence in transcript."


def _extract_next_step(open_loop: str) -> str:
    if open_loop and open_loop != "Not enough evidence in transcript.":
        return _truncate(f"Read the latest user message first; if continuing this thread, use this context: {open_loop}", 700)
    return "Read the latest user message first; only continue stale work if the user explicitly asks."


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
    counts = _quality_counts(raw_messages, include_tool_results=include_tool_results)
    visible = _visible_messages(raw_messages, include_tool_results=include_tool_results)
    bounded_limit = max(1, min(int(max_messages or 1), 200))
    bounded_messages = visible[-bounded_limit:]
    messages_omitted_by_limit = max(0, len(visible) - len(bounded_messages))
    per_message_chars = max(120, min(1200, int(max_chars / max(1, len(bounded_messages) or 1))))
    evidence, evidence_truncated = _evidence_tail(
        bounded_messages,
        max_messages=bounded_limit,
        per_message_chars=per_message_chars,
    )
    evidence_inventory = _extract_evidence_inventory(bounded_messages)

    latest_user = _latest_role_text(bounded_messages, "user", 700)
    latest_assistant = _latest_role_text(bounded_messages, "assistant", 900)
    raw_latest_assistant = _latest_role_raw_text(bounded_messages, "assistant")
    completed_actions = _extract_completed_actions(raw_latest_assistant)
    last_completed_action = (
        "; ".join(completed_actions) if completed_actions else _extract_last_completed_action(raw_latest_assistant)
    )
    open_loops = _extract_open_loop(latest_user, raw_latest_assistant)
    next_concrete_step = _extract_next_step(open_loops)
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

    evidence_lines = [f"- {item['role']}: {item['content']}" for item in evidence] or ["- No transcript evidence available."]

    def make_quality_card(*, max_chars_applied: bool) -> dict[str, Any]:
        return {
            **counts,
            "visible_message_count": len(visible),
            "bounded_message_count": len(bounded_messages),
            "messages_omitted_by_limit": messages_omitted_by_limit,
            "evidence_count": len(evidence),
            "structured_file_count": len(evidence_inventory["files"]),
            "structured_command_count": len(evidence_inventory["commands"]),
            "structured_config_key_count": len(evidence_inventory["config_keys"]),
            "structured_commit_count": len(evidence_inventory["commits"]),
            "truncated": bool(evidence_truncated or max_chars_applied),
            "include_tool_results": include_tool_results,
        }

    def make_handoff_quality(*, max_chars_applied: bool) -> dict[str, Any]:
        return {
            "raw_message_count": counts["raw_message_count"],
            "visible_message_count": len(visible),
            "meta_messages_filtered": counts["filtered_meta_messages"],
            "tool_messages_excluded": counts["tool_messages_excluded"],
            "truncation": {
                "max_messages_applied": messages_omitted_by_limit > 0,
                "max_chars_applied": bool(evidence_truncated or max_chars_applied),
            },
            "extraction": {
                "completed_action_detector": "bullet_and_newline_aware",
                "open_loop_detector": "bullet_and_newline_aware",
                "latest_user_fallback_suppressed_in_remote_preview": True,
            },
        }

    def render_markdown(quality_card: Mapping[str, Any], handoff_quality: Mapping[str, Any]) -> str:
        truncation_obj = handoff_quality.get("truncation")
        truncation = truncation_obj if isinstance(truncation_obj, Mapping) else {}
        extraction_obj = handoff_quality.get("extraction")
        extraction = extraction_obj if isinstance(extraction_obj, Mapping) else {}
        return f"""[SESSION HANDOFF — REFERENCE ONLY]
This handoff is background reference from a previous Hermes session. The latest user message in the new session wins. Do not execute stale tasks unless the user asks to continue them.

## Fresh Session Prompt
Please continue from the local handoff artifact at: {artifact_path_text}
Treat it as reference only. Preserve the Intent Locks. First restate the next concrete step, then act only on my latest instruction.

## Intent Locks
{_format_bullets(intent_locks)}

## Handoff Quality
- Raw messages: {quality_card['raw_message_count']}
- Visible messages: {quality_card['visible_message_count']}
- Evidence kept: {quality_card['evidence_count']}
- Tool messages excluded: {quality_card['tool_messages_excluded']}
- Meta messages filtered: {quality_card['filtered_meta_messages']}
- Max messages applied: {str(bool(truncation.get('max_messages_applied'))).lower()}
- Max chars applied: {str(bool(truncation.get('max_chars_applied'))).lower()}
- completed_action_detector: {extraction.get('completed_action_detector', 'unknown')}
- open_loop_detector: {extraction.get('open_loop_detector', 'unknown')}
- latest_user_fallback_suppressed_in_remote_preview: {str(bool(extraction.get('latest_user_fallback_suppressed_in_remote_preview'))).lower()}
- Truncated: {str(quality_card['truncated']).lower()}

## Last Completed Action
{_format_bullets(completed_actions or [last_completed_action])}

## Open Loops / Follow-up Context
- {_truncate(open_loops, 700)}

## Next Useful Context
- Latest user signal: {_truncate(latest_user, 700)}
- Latest assistant signal: {_truncate(latest_assistant, 900)}

## Current State
- Previous session id: {session_id}
- New session id: {new_session_id or 'not created when handoff was built'}
- Platform: {platform or 'unknown'}
- Source: {source_label or 'unknown'}

## Decisions Made
- Treat this artifact as reference only; latest user message overrides it.
- Keep handoff content local/private unless explicit external sharing approval is given.

## Files / Repos / Commands Involved
{_format_inventory_section(evidence_inventory)}

## Known Failure Modes
{_format_bullets(known_failure_modes)}

## Next Concrete Step
- {next_concrete_step}

## Evidence Tail
{chr(10).join(evidence_lines)}
"""

    quality_card = make_quality_card(max_chars_applied=False)
    handoff_quality = make_handoff_quality(max_chars_applied=False)
    markdown = render_markdown(quality_card, handoff_quality)
    markdown_truncated = len(markdown) > max_chars
    if markdown_truncated:
        quality_card = make_quality_card(max_chars_applied=True)
        handoff_quality = make_handoff_quality(max_chars_applied=True)
        markdown = render_markdown(quality_card, handoff_quality)
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
        "active_task": open_loops
        if open_loops != "Not enough evidence in transcript."
        else "Not enough deterministic active task evidence in transcript.",
        "last_completed_action": last_completed_action,
        "completed_actions": completed_actions,
        "open_loops": open_loops,
        "next_concrete_step": next_concrete_step,
        "quality_card": quality_card,
        "handoff_quality": handoff_quality,
        "evidence_inventory": evidence_inventory,
        "current_state": {
            "latest_user_signal": latest_user,
            "latest_assistant_evidence": latest_assistant,
            "message_count": len(raw_messages),
            "evidence_count": len(evidence),
        },
        "evidence_tail": evidence,
    }
    return HandoffArtifact(markdown=markdown, json_payload=payload, title=title)


def build_handoff_preview(artifact: HandoffArtifact, *, max_items: int = 4, max_chars: int = 600) -> str:
    """Build a bounded reset-reply preview without dumping transcript evidence."""

    payload = artifact.json_payload
    quality_obj = payload.get("quality_card")
    quality: Mapping[str, Any] = quality_obj if isinstance(quality_obj, Mapping) else {}
    max_items = max(1, min(int(max_items or 1), 6))
    max_chars = max(160, min(int(max_chars or 600), 1200))

    last_completed = str(payload.get("last_completed_action") or "")
    open_loop = str(payload.get("open_loops") or "")
    has_completion = bool(last_completed) and not last_completed.startswith("Not enough")
    has_follow_up = bool(open_loop) and not open_loop.startswith("Not enough")
    if has_follow_up and open_loop.startswith("Latest user signal:"):
        open_loop_status = "latest user message captured in local handoff; inspect path for details"
    elif has_follow_up:
        open_loop_status = "follow-up context captured in local handoff; inspect path for details"
    else:
        open_loop_status = "no deterministic follow-up found"

    lines = ["Preview:"]
    structured_counts = (
        f"; structured files={quality.get('structured_file_count', 0)} "
        f"commands={quality.get('structured_command_count', 0)} "
        f"commits={quality.get('structured_commit_count', 0)}"
        if any(
            int(quality.get(key, 0) or 0)
            for key in ("structured_file_count", "structured_command_count", "structured_commit_count")
        )
        else ""
    )
    candidates = [
        (
            "Last completed",
            "completion evidence captured in local handoff; inspect path for details"
            if has_completion
            else "no deterministic completion evidence found",
        ),
        ("Open loop", open_loop_status),
        (
            "Evidence",
            (
                f"{quality.get('evidence_count', 0)} kept; "
                f"{quality.get('filtered_meta_messages', 0)} meta filtered; "
                f"{quality.get('tool_messages_excluded', 0)} tool excluded; "
                f"truncated={str(bool(quality.get('truncated', False))).lower()}"
                f"{structured_counts}"
            ),
        ),
        ("Inspect", "ask Hermes to read the Handoff path for full local detail"),
    ]
    for label, value in candidates[:max_items]:
        lines.append(f"- {label}: {_truncate(str(value), 180)}")
    preview = "\n".join(lines)
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1].rstrip() + "…"
    return preview


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
