"""Host-bound, raw-free authority for one foreground user turn.

Only the conversation host binds this context. Model arguments, retrieved
text, plans, assistant text, background turns, and delegated workers cannot
mint mutation authority. The token carries digests and host scope only; the
trusted user text stays in a separate turn-local host context.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

_HEX_64_RE = re.compile(r"^[a-f0-9]{64}$")
_HOST_KEY = secrets.token_bytes(32)
_BLOCKED_SURFACES = frozenset(
    {
        "api_server",
        "background",
        "cron",
        "delegate",
        "gateway",
        "kanban",
        "local",
        "msgraph_webhook",
        "review",
        "subagent",
        "tool",
        "webhook",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class CurrentTurnUserAuthority:
    """Raw-free proof attached to one accepted ``role=user`` turn."""

    turn_id: str
    source_role: str
    session_scope: str
    platform_scope: str
    user_message_index: int
    user_action_fingerprint: str
    host_signature: str

    @property
    def source_event_fingerprint(self) -> str:
        """Compatibility alias used by external fixed-action owners."""

        return self.user_action_fingerprint

    def __repr__(self) -> str:
        return "CurrentTurnUserAuthority(<raw-free>)"


_CURRENT_AUTHORITY: ContextVar[CurrentTurnUserAuthority | None] = ContextVar(
    "HERMES_CURRENT_TURN_WORKFLOW_AUTHORITY",
    default=None,
)
_ACTIVE_TURN: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "HERMES_ACTIVE_WORKFLOW_TURN",
    default=None,
)


def _normalize(value: str, name: str) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"{name}_invalid")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def fingerprint_user_action(user_message: str) -> str:
    """Return a stable digest without retaining the user text."""

    return hashlib.sha256(
        _normalize(user_message, "user_message").encode("utf-8")
    ).hexdigest()


def fingerprint_workflow_target(target: str) -> str:
    return hashlib.sha256(_normalize(target, "target").encode("utf-8")).hexdigest()


def _signature(
    *,
    turn_id: str,
    source_role: str,
    session_scope: str,
    platform_scope: str,
    user_message_index: int,
    user_action_fingerprint: str,
) -> str:
    payload = "\0".join(
        (
            turn_id,
            source_role,
            session_scope,
            platform_scope,
            str(user_message_index),
            user_action_fingerprint,
        )
    ).encode("utf-8")
    return hmac.new(_HOST_KEY, payload, hashlib.sha256).hexdigest()


def _bind_host_current_turn_user_authority(
    trusted_user_message: str,
    *,
    turn_id: str,
    session_scope: str,
    platform_scope: str,
    user_message_index: int,
    source_role: str = "user",
) -> CurrentTurnUserAuthority:
    """Bind authority from the trusted conversation prologue.

    This is deliberately private and is not a tool or model-callable surface.
    The caller must already have rejected synthetic/internal turns.
    """

    normalized_platform = _normalize(platform_scope, "platform_scope")
    if normalized_platform in _BLOCKED_SURFACES:
        raise ValueError("authority_surface_invalid")
    if source_role != "user":
        raise ValueError("authority_source_invalid")
    if type(user_message_index) is not int or user_message_index < 0:
        raise ValueError("user_message_index_invalid")
    normalized_turn = _normalize(turn_id, "turn_id")
    normalized_session = _normalize(session_scope, "session_scope")
    action_fingerprint = fingerprint_user_action(trusted_user_message)
    authority = CurrentTurnUserAuthority(
        turn_id=normalized_turn,
        source_role=source_role,
        session_scope=normalized_session,
        platform_scope=normalized_platform,
        user_message_index=user_message_index,
        user_action_fingerprint=action_fingerprint,
        host_signature=_signature(
            turn_id=normalized_turn,
            source_role=source_role,
            session_scope=normalized_session,
            platform_scope=normalized_platform,
            user_message_index=user_message_index,
            user_action_fingerprint=action_fingerprint,
        ),
    )
    _CURRENT_AUTHORITY.set(authority)
    _ACTIVE_TURN.set((normalized_turn, normalized_platform, normalized_session))

    from gateway.session_context import _bind_trusted_current_user_context

    _bind_trusted_current_user_context(
        trusted_user_message,
        controller_role="main_controller",
    )
    return authority


def is_host_issued_current_turn_authority(authority: object) -> bool:
    if type(authority) is not CurrentTurnUserAuthority:
        return False
    if (
        type(authority.turn_id) is not str
        or not authority.turn_id
        or authority.source_role != "user"
        or type(authority.session_scope) is not str
        or not authority.session_scope
        or type(authority.platform_scope) is not str
        or not authority.platform_scope
        or authority.platform_scope in _BLOCKED_SURFACES
        or type(authority.user_message_index) is not int
        or authority.user_message_index < 0
        or _HEX_64_RE.fullmatch(authority.user_action_fingerprint) is None
        or _HEX_64_RE.fullmatch(authority.host_signature) is None
    ):
        return False
    expected = _signature(
        turn_id=authority.turn_id,
        source_role=authority.source_role,
        session_scope=authority.session_scope,
        platform_scope=authority.platform_scope,
        user_message_index=authority.user_message_index,
        user_action_fingerprint=authority.user_action_fingerprint,
    )
    return hmac.compare_digest(authority.host_signature, expected)


def get_current_turn_user_authority() -> CurrentTurnUserAuthority | None:
    authority = _CURRENT_AUTHORITY.get()
    return authority if is_host_issued_current_turn_authority(authority) else None


def matches_active_workflow_turn(
    authority: object,
    *,
    user_message: str | None = None,
    session_id: str | None = None,
) -> bool:
    """Match the host-bound active turn; optional arguments tighten the match."""

    if not is_host_issued_current_turn_authority(authority):
        return False
    assert type(authority) is CurrentTurnUserAuthority
    if _ACTIVE_TURN.get() != (
        authority.turn_id,
        authority.platform_scope,
        authority.session_scope,
    ):
        return False
    if user_message is not None:
        try:
            if not hmac.compare_digest(
                authority.user_action_fingerprint,
                fingerprint_user_action(user_message),
            ):
                return False
        except (TypeError, ValueError):
            return False
    if session_id is not None:
        if type(session_id) is not str or not hmac.compare_digest(
            authority.session_scope,
            _normalize(session_id, "session_id"),
        ):
            return False
    return True


def matches_current_workflow_session(authority: object) -> bool:
    """Require the active authority to match independently bound host scope."""

    if not matches_active_workflow_turn(authority) or os.environ.get("HERMES_KANBAN_TASK"):
        return False
    assert type(authority) is CurrentTurnUserAuthority

    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import get_session_env

    if (
        is_delegated_child_context()
        or get_session_env("HERMES_CRON_SESSION", "") == "1"
    ):
        return False
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().casefold()
    source = get_session_env("HERMES_SESSION_SOURCE", "").strip().casefold()
    session_id = get_session_env("HERMES_SESSION_ID", "").strip().casefold()
    host_surface = platform or source
    if host_surface and host_surface in _BLOCKED_SURFACES:
        return False
    if host_surface and not hmac.compare_digest(authority.platform_scope, host_surface):
        return False
    if session_id and not hmac.compare_digest(authority.session_scope, session_id):
        return False
    return bool(host_surface or authority.platform_scope in {"cli", "desktop", "manual", "tui"})


def opaque_workflow_action_id() -> str:
    return f"opaque-action-{secrets.token_hex(16)}"


def clear_current_turn_user_authority() -> None:
    _CURRENT_AUTHORITY.set(None)
    _ACTIVE_TURN.set(None)
    try:
        from gateway.session_context import _clear_trusted_current_user_context

        _clear_trusted_current_user_context()
    except Exception:
        pass


@contextmanager
def _scoped_test_current_turn_user_authority(
    user_message: str,
    *,
    session_id: str,
    turn_id: str = "synthetic-turn",
    platform_scope: str = "manual",
    user_message_index: int = 0,
) -> Iterator[CurrentTurnUserAuthority]:
    """Synthetic host boundary for focused tests; never exposed as a tool."""

    clear_current_turn_user_authority()
    try:
        yield _bind_host_current_turn_user_authority(
            user_message,
            turn_id=turn_id,
            session_scope=session_id,
            platform_scope=platform_scope,
            user_message_index=user_message_index,
        )
    finally:
        clear_current_turn_user_authority()


__all__ = [
    "CurrentTurnUserAuthority",
    "clear_current_turn_user_authority",
    "fingerprint_user_action",
    "fingerprint_workflow_target",
    "get_current_turn_user_authority",
    "is_host_issued_current_turn_authority",
    "matches_active_workflow_turn",
    "matches_current_workflow_session",
    "opaque_workflow_action_id",
]
