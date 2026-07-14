"""Hidden current-turn user-authority provenance for typed workflow tools.

The context value contains no user text and is not accepted as a model/tool
argument. It proves execution is attached to the accepted current ``role=user``
turn; domain-specific validation and terminal hard guards remain separate.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from contextvars import ContextVar, Token
from dataclasses import dataclass

_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
_TASK_ID_RE = re.compile(r"\bt_[a-z0-9]{6,64}\b", re.IGNORECASE)
_QUOTED_TARGET_RE = re.compile(
    r'''(?:"([^"]{1,200})"|'([^']{1,200})'|`([^`]{1,200})`|“([^”]{1,200})”)'''
)
_ALLOWED_ACTION_CLASSES = frozenset(
    {"status_memory", "explicit_blocked_card_create", "trusted_local_record"}
)


def fingerprint_user_action(user_message: str) -> str:
    """Return a stable raw-free fingerprint of the accepted user action text."""
    if not isinstance(user_message, str) or not user_message.strip():
        raise ValueError("user_message must be non-empty")
    normalized = unicodedata.normalize("NFKC", user_message).strip().casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fingerprint_workflow_target(target: str) -> str:
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target must be non-empty")
    normalized = unicodedata.normalize("NFKC", target).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def infer_explicit_blocked_create_targets(user_message: str) -> frozenset[str]:
    """Return raw-free fingerprints for explicitly quoted create targets."""
    if not isinstance(user_message, str):
        return frozenset()
    targets: set[str] = set()
    for match in _QUOTED_TARGET_RE.finditer(user_message):
        value = next((group for group in match.groups() if group), "")
        if value.strip():
            targets.add(fingerprint_workflow_target(value))
    return frozenset(targets)


def infer_explicit_workflow_scope(
    user_message: str,
) -> tuple[frozenset[str], frozenset[str]]:
    """Infer only narrow explicit workflow grants; ambiguity yields no grant."""
    normalized = unicodedata.normalize("NFKC", user_message).strip().casefold()
    classes: set[str] = set()
    mentions_card = any(token in normalized for token in ("card", "kanban", "카드"))
    if mentions_card and any(
        token in normalized
        for token in ("comment", "note", "댓글", "코멘트", "상태 반영", "기록")
    ):
        classes.add("status_memory")
    if mentions_card and any(
        token in normalized for token in ("create", "make", "생성", "만들")
    ):
        classes.add("explicit_blocked_card_create")
    if any(token in normalized for token in ("기록해", "기록해줘", "record")) and any(
        token in normalized
        for token in ("식사", "먹었", "복약", "약", "건강", "diet", "medication")
    ):
        classes.add("trusted_local_record")
    targets = frozenset(
        fingerprint_workflow_target(match.group(0))
        for match in _TASK_ID_RE.finditer(normalized)
    )
    return frozenset(classes), targets


@dataclass(frozen=True, slots=True)
class CurrentTurnUserAuthority:
    turn_id: str
    source_role: str
    session_scope: str
    platform_scope: str
    user_message_index: int
    user_action_fingerprint: str = ""
    allowed_action_classes: frozenset[str] = frozenset()
    target_fingerprints: frozenset[str] = frozenset()
    blocked_create_target_fingerprints: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.source_role != "user":
            raise ValueError("source_role must be the accepted current user role")
        if not self.turn_id:
            raise ValueError("turn_id must be non-empty")
        if self.user_message_index < 0:
            raise ValueError("user_message_index must be non-negative")
        if self.user_action_fingerprint and not _FINGERPRINT_RE.fullmatch(
            self.user_action_fingerprint
        ):
            raise ValueError("user_action_fingerprint must be a SHA-256 token")
        if not self.allowed_action_classes.issubset(_ALLOWED_ACTION_CLASSES):
            raise ValueError("allowed_action_classes contains an unknown class")
        if any(not _FINGERPRINT_RE.fullmatch(value) for value in self.target_fingerprints):
            raise ValueError("target_fingerprints must contain SHA-256 tokens")
        if any(
            not _FINGERPRINT_RE.fullmatch(value)
            for value in self.blocked_create_target_fingerprints
        ):
            raise ValueError(
                "blocked_create_target_fingerprints must contain SHA-256 tokens"
            )

    def allows(self, action_class: str, target: str | None = None) -> bool:
        if action_class not in self.allowed_action_classes:
            return False
        if target is None:
            return True
        return fingerprint_workflow_target(target) in self.target_fingerprints


def select_blocked_create_target_fingerprint(
    authority: CurrentTurnUserAuthority, proposed_title: str
) -> str:
    """Select a user-derived discriminator without hashing assistant prose."""
    candidates = authority.blocked_create_target_fingerprints
    if not candidates:
        return authority.user_action_fingerprint
    if len(candidates) == 1:
        return next(iter(candidates))
    proposed = fingerprint_workflow_target(proposed_title)
    if proposed not in candidates:
        raise ValueError("requested blocked-card target is not authorized")
    return proposed


_current_turn_user_authority: ContextVar[CurrentTurnUserAuthority | None] = ContextVar(
    "current_turn_user_authority", default=None
)


def bind_current_turn_user_authority(
    authority: CurrentTurnUserAuthority,
) -> Token[CurrentTurnUserAuthority | None]:
    if not isinstance(authority, CurrentTurnUserAuthority):
        raise TypeError("authority must be CurrentTurnUserAuthority")
    return _current_turn_user_authority.set(authority)


def reset_current_turn_user_authority(
    token: Token[CurrentTurnUserAuthority | None],
) -> None:
    _current_turn_user_authority.reset(token)


def get_current_turn_user_authority() -> CurrentTurnUserAuthority | None:
    return _current_turn_user_authority.get()


def opaque_workflow_action_id(
    authority: CurrentTurnUserAuthority | None,
) -> str:
    """Return an opaque action id without resource, card, or payload identifiers."""
    if authority is not None and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", authority.turn_id):
        return authority.turn_id
    return f"opaque-action-{secrets.token_hex(16)}"


def clear_current_turn_user_authority() -> None:
    _current_turn_user_authority.set(None)
