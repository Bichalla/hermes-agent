"""Default-off current-turn authority tokens for fixed workflow adapters.

This module stores no user text. It binds a fixed-action workflow request to a
foreground user turn by digest only, so retrieved text, plan text, assistant
text, or model-provided arguments cannot become mutation authority.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

_HEX_64_RE = re.compile(r"^[a-f0-9]{64}$")
_HOST_KEY = secrets.token_bytes(32)
_CURRENT_AUTHORITY: ContextVar["CurrentTurnUserAuthority | None"] = ContextVar(
    "HERMES_CURRENT_TURN_WORKFLOW_AUTHORITY",
    default=None,
)


@dataclass(frozen=True, slots=True, repr=False)
class CurrentTurnUserAuthority:
    """Raw-free proof that one fixed workflow request is tied to this user turn."""

    user_action_fingerprint: str
    session_fingerprint: str
    source: str
    host_signature: str

    def __repr__(self) -> str:
        return "CurrentTurnUserAuthority(<raw-free>)"


def _normalize(value: str, name: str) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"{name}_invalid")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def fingerprint_user_action(user_message: str) -> str:
    """Return a stable digest for the accepted foreground user action."""

    return hashlib.sha256(_normalize(user_message, "user_message").encode("utf-8")).hexdigest()


def _session_fingerprint(session_id: str) -> str:
    return hashlib.sha256(_normalize(session_id, "session_id").encode("utf-8")).hexdigest()


def _signature(action_fingerprint: str, session_fingerprint: str, source: str) -> str:
    payload = f"{source}\0{session_fingerprint}\0{action_fingerprint}".encode("ascii")
    return hmac.new(_HOST_KEY, payload, hashlib.sha256).hexdigest()


def issue_current_turn_user_authority(
    user_message: str,
    *,
    session_id: str,
    source: str = "foreground_user",
) -> CurrentTurnUserAuthority:
    """Issue an authority token only for a host-accepted foreground user turn."""

    if source != "foreground_user":
        raise ValueError("authority_source_invalid")
    action_fingerprint = fingerprint_user_action(user_message)
    session_digest = _session_fingerprint(session_id)
    authority = CurrentTurnUserAuthority(
        user_action_fingerprint=action_fingerprint,
        session_fingerprint=session_digest,
        source=source,
        host_signature=_signature(action_fingerprint, session_digest, source),
    )
    _CURRENT_AUTHORITY.set(authority)
    return authority


def is_host_issued_current_turn_authority(authority: object) -> bool:
    if type(authority) is not CurrentTurnUserAuthority:
        return False
    if (
        not _HEX_64_RE.fullmatch(authority.user_action_fingerprint)
        or not _HEX_64_RE.fullmatch(authority.session_fingerprint)
        or authority.source != "foreground_user"
        or not _HEX_64_RE.fullmatch(authority.host_signature)
    ):
        return False
    expected = _signature(
        authority.user_action_fingerprint,
        authority.session_fingerprint,
        authority.source,
    )
    return hmac.compare_digest(authority.host_signature, expected)


def get_current_turn_user_authority() -> CurrentTurnUserAuthority | None:
    authority = _CURRENT_AUTHORITY.get()
    if is_host_issued_current_turn_authority(authority):
        return authority
    return None


def matches_active_workflow_turn(
    authority: object,
    *,
    user_message: str,
    session_id: str,
) -> bool:
    if not is_host_issued_current_turn_authority(authority):
        return False
    assert type(authority) is CurrentTurnUserAuthority
    return (
        hmac.compare_digest(authority.user_action_fingerprint, fingerprint_user_action(user_message))
        and hmac.compare_digest(authority.session_fingerprint, _session_fingerprint(session_id))
    )


@contextmanager
def scoped_current_turn_user_authority(
    user_message: str,
    *,
    session_id: str,
) -> Iterator[CurrentTurnUserAuthority]:
    token = _CURRENT_AUTHORITY.set(None)
    try:
        authority = issue_current_turn_user_authority(
            user_message,
            session_id=session_id,
        )
        yield authority
    finally:
        _CURRENT_AUTHORITY.reset(token)


__all__ = [
    "CurrentTurnUserAuthority",
    "fingerprint_user_action",
    "get_current_turn_user_authority",
    "is_host_issued_current_turn_authority",
    "issue_current_turn_user_authority",
    "matches_active_workflow_turn",
    "scoped_current_turn_user_authority",
]
