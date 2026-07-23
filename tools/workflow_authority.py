"""Hidden current-turn user-authority provenance for typed workflow tools.

The context value contains no user text and is not accepted as a model/tool
argument. It proves execution is attached to the accepted current ``role=user``
turn; domain-specific validation and terminal hard guards remain separate.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import unicodedata
from contextvars import ContextVar, Token
from dataclasses import dataclass

_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
_TASK_ID_RE = re.compile(r"(?<![a-z0-9])t_[a-z0-9]{6,64}(?![a-z0-9])", re.IGNORECASE)
_PENDING_ID_RE = re.compile(r"(?<![a-f0-9])kp_[a-f0-9]{16}(?![a-f0-9])")
_QUOTED_TARGET_RE = re.compile(
    r'''(?:"([^"]{1,200})"|'([^']{1,200})'|`([^`]{1,200})`|“([^”]{1,200})”)'''
)
_ALLOWED_ACTION_CLASSES = frozenset(
    {
        "status_memory",
        "explicit_blocked_card_create",
        "registered_soft_delete",
    }
)
_ALLOWED_OPERATIONS = frozenset(
    {
        "pending_read",
        "pending_soft_delete",
        "pending_restore",
        "kanban_status_memory_comment",
    }
)

_NEGATION_TOKENS = (
    "never",
    "should not",
    "shouldn't",
    "do not",
    "don't",
    "dont",
    "did not",
    "didn't",
    "not delete",
    "not restore",
    "하지 마",
    "하지마",
    "하지 말",
    "말고",
    "삭제하지",
    "복원하지",
    "기록하지",
    " 안 ",
    "안 했",
    "안 해",
)
_NON_COMMAND_TOKENS = (
    "?",
    "should i",
    "can i",
    "could i",
    "how to",
    "explain",
    "what if",
    "어떻게",
    "방법",
    "할까",
    "할까요",
    "해도 될까",
)
_REPORTED_SPEECH_TOKENS = (
    "라고 말",
    "라고 요청",
    "라고 기록",
    "라고 설명",
    "라며 말",
    "라며 요청",
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
    grants = infer_explicit_workflow_grants(user_message)
    grant_operations = {operation for operation, _target in grants}
    if "kanban_status_memory_comment" in grant_operations:
        classes.add("status_memory")
    if _is_direct_blocked_create_command(normalized):
        classes.add("explicit_blocked_card_create")

    pending_ids = tuple(_PENDING_ID_RE.finditer(normalized))
    if pending_ids and any(
        operation.startswith("pending_") for operation in grant_operations
    ):
        classes.add("registered_soft_delete")
    targets = frozenset(
        fingerprint_workflow_target(match.group(0))
        for match in (*tuple(_TASK_ID_RE.finditer(normalized)), *pending_ids)
    )
    return frozenset(classes), targets


def infer_explicit_workflow_operations(user_message: str) -> frozenset[str]:
    """Return operations from exact affirmative operation-target grants."""
    return frozenset(operation for operation, _target in infer_explicit_workflow_grants(user_message))


def _has_negation(normalized: str) -> bool:
    return any(token in normalized for token in _NEGATION_TOKENS)


def _is_affirmative_command(normalized: str) -> bool:
    if (
        not normalized
        or _has_negation(normalized)
        or any(token in normalized for token in _NON_COMMAND_TOKENS)
        or any(token in normalized for token in _REPORTED_SPEECH_TOKENS)
    ):
        return False
    english_starts = (
        "record ",
        "add ",
        "write ",
        "create ",
        "make ",
        "dismiss ",
        "restore ",
        "undo ",
        "read ",
        "show ",
        "inspect ",
        "soft delete ",
    )
    korean_or_polite_endings = (
        "해줘",
        "해주세요",
        "기록",
        "기록해",
        "기록해줘",
        "기록해주세요",
        "남겨",
        "남겨줘",
        "추가해",
        "추가해줘",
        "업데이트해",
        "업데이트해줘",
        "첨부해",
        "첨부해줘",
        "만들어",
        "만들어라",
        "만들어줘",
        "생성해",
        "생성해라",
        "생성해줘",
        "삭제",
        "삭제해",
        "삭제해줘",
        "지워줘",
        "제외해줘",
        "복원해",
        "복원해줘",
        "되돌려줘",
        "살려줘",
        "조회해줘",
        "조회",
        "확인해줘",
        "보여줘",
        " please",
        " now",
    )
    # A direct command often has a short explanatory sentence after it
    # (for example, "카드 만들어라. 이제 될 거야."). Keep whole-message
    # negation/question guards above, then accept any imperative clause.
    clauses = tuple(
        clause.strip()
        for clause in re.split(r"[.!?。！？\n]+", normalized)
        if clause.strip()
    )
    return any(
        clause.startswith(english_starts) or clause.endswith(korean_or_polite_endings)
        for clause in clauses
    )


def _is_direct_blocked_create_command(normalized: str) -> bool:
    """Recognize a create imperative whose verb belongs to the card clause."""
    if (
        not normalized
        or _has_negation(normalized)
        or any(token in normalized for token in _NON_COMMAND_TOKENS)
        or any(token in normalized for token in _REPORTED_SPEECH_TOKENS)
    ):
        return False
    clauses = tuple(
        clause.strip(" \t\"'“”‘’")
        for clause in re.split(r"[.!?。！？\n]+", normalized)
        if clause.strip()
    )
    english_card_object = re.compile(
        r"^(?:please\s+)?(?:create|make|add)\s+"
        r"(?:(?:a|an|the|one|new|blocked|kanban)\s+)*card\b"
    )
    korean_card_object = re.compile(
        r"카드(?:를|은|는|도|로)?"
        r"(?:\s+(?:하나(?:만)?|한\s*장|1\s*장|새로|다시|바로|좀|먼저))*\s+"
        r"(?:만들어|만들어라|만들어줘|만들어주세요|생성해|생성해라|생성해줘|생성해주세요)$"
    )
    for clause in clauses:
        if english_card_object.match(clause):
            return True
        if korean_card_object.search(clause):
            return True
    return False


def infer_explicit_workflow_grants(
    user_message: str,
) -> frozenset[tuple[str, str]]:
    """Infer exact affirmative ``(operation, target fingerprint)`` grants.

    Multiple pending IDs or multiple lifecycle verbs are deliberately
    ambiguous and mint no pending grant.
    """
    if not isinstance(user_message, str):
        return frozenset()
    normalized = unicodedata.normalize("NFKC", user_message).strip().casefold()
    if not _is_affirmative_command(normalized):
        return frozenset()
    grants: set[tuple[str, str]] = set()

    task_ids = tuple(match.group(0) for match in _TASK_ID_RE.finditer(normalized))
    status_memory_kind = any(
        token in normalized
        for token in (
            "댓글",
            "코멘트",
            "comment",
            "note",
            "verification summary",
            "progress update",
            "status update",
            "artifact link",
            "repo url",
            "repository url",
            "pr url",
            "handoff note",
            "검증 요약",
            "진행 상황",
            "진행 업데이트",
            "상태 업데이트",
            "아티팩트 링크",
            "결과 링크",
            "저장소 링크",
            "pr 링크",
            "인계 메모",
            "핸드오프",
        )
    )
    if (
        len(task_ids) == 1
        and status_memory_kind
        and any(
            token in normalized
            for token in (
                "기록",
                "기록해",
                "남겨",
                "추가해",
                "업데이트",
                "첨부",
                "연결",
                "add",
                "write",
                "update",
                "attach",
                "link",
            )
        )
    ):
        grants.add(
            (
                "kanban_status_memory_comment",
                fingerprint_workflow_target(task_ids[0]),
            )
        )
    pending_ids = tuple(match.group(0) for match in _PENDING_ID_RE.finditer(normalized))
    if len(pending_ids) != 1:
        return frozenset(grants)
    dismiss = any(
        token in normalized
        for token in ("soft delete", "soft-delete", "dismiss", "삭제", "지워", "제외")
    )
    restore = any(
        token in normalized for token in ("restore", "undo", "복원", "되돌려", "살려")
    )
    read = any(
        token in normalized
        for token in ("read", "show", "inspect", "조회", "확인", "보여")
    )
    operations = [
        operation
        for operation, matched in (
            ("pending_soft_delete", dismiss),
            ("pending_restore", restore),
            ("pending_read", read),
        )
        if matched
    ]
    if len(operations) != 1:
        return frozenset(grants)
    grants.add((operations[0], fingerprint_workflow_target(pending_ids[0])))
    return frozenset(grants)


def infer_coarse_estimate_authority(user_message: str) -> bool:
    if not isinstance(user_message, str):
        return False
    normalized = unicodedata.normalize("NFKC", user_message).strip().casefold()
    if _has_negation(normalized):
        return False
    return any(
        token in normalized
        for token in (
            "rough estimate okay",
            "coarse estimate authorized",
            "대략 추정 허용",
            "대략 계산해",
            "영양 추정해",
        )
    )


@dataclass(frozen=True, slots=True)
class CurrentTurnUserAuthority:
    turn_id: str
    source_role: str
    session_scope: str
    platform_scope: str
    user_message_index: int
    user_action_fingerprint: str = ""
    source_event_fingerprint: str = ""
    allowed_action_classes: frozenset[str] = frozenset()
    allowed_operations: frozenset[str] = frozenset()
    operation_target_grants: frozenset[tuple[str, str]] = frozenset()
    target_fingerprints: frozenset[str] = frozenset()
    blocked_create_target_fingerprints: frozenset[str] = frozenset()
    coarse_estimate_authorized: bool = False

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
        if self.source_event_fingerprint and not _FINGERPRINT_RE.fullmatch(
            self.source_event_fingerprint
        ):
            raise ValueError("source_event_fingerprint must be a SHA-256 token")
        if not self.allowed_action_classes.issubset(_ALLOWED_ACTION_CLASSES):
            raise ValueError("allowed_action_classes contains an unknown class")
        if not self.allowed_operations.issubset(_ALLOWED_OPERATIONS):
            raise ValueError("allowed_operations contains an unknown operation")
        if type(self.coarse_estimate_authorized) is not bool:
            raise ValueError("coarse_estimate_authorized must be bool")
        for grant in self.operation_target_grants:
            if (
                type(grant) is not tuple
                or len(grant) != 2
                or grant[0] not in _ALLOWED_OPERATIONS
                or not _FINGERPRINT_RE.fullmatch(grant[1])
            ):
                raise ValueError("operation_target_grants contains an invalid grant")
        if any(not _FINGERPRINT_RE.fullmatch(value) for value in self.target_fingerprints):
            raise ValueError("target_fingerprints must contain SHA-256 tokens")
        if any(
            not _FINGERPRINT_RE.fullmatch(value)
            for value in self.blocked_create_target_fingerprints
        ):
            raise ValueError(
                "blocked_create_target_fingerprints must contain SHA-256 tokens"
            )

    def allows(
        self,
        action_class: str,
        target: str | None = None,
        *,
        operation: str | None = None,
    ) -> bool:
        if action_class not in self.allowed_action_classes:
            return False
        if operation is not None and operation not in self.allowed_operations:
            return False
        if target is None:
            return True
        return fingerprint_workflow_target(target) in self.target_fingerprints

    def allows_operation_target(self, operation: str, target: str) -> bool:
        return (
            operation,
            fingerprint_workflow_target(target),
        ) in self.operation_target_grants


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
_active_workflow_turn: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "active_workflow_turn", default=None
)
_active_turn_lock = threading.RLock()
_active_turn_registry: dict[tuple[str, str], str] = {}


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


def bind_active_workflow_turn(turn_id: str, platform: str, session_id: str) -> None:
    if not all(type(value) is str and value for value in (turn_id, platform, session_id)):
        raise ValueError("active workflow turn fields must be non-empty strings")
    normalized_platform = platform.strip().lower()
    with _active_turn_lock:
        _active_turn_registry[(normalized_platform, session_id)] = turn_id
    _active_workflow_turn.set((turn_id, normalized_platform, session_id))


def matches_active_workflow_turn(authority: CurrentTurnUserAuthority) -> bool:
    active = _active_workflow_turn.get()
    if active is None:
        return False
    expected = (
        authority.turn_id,
        authority.platform_scope.strip().lower(),
        authority.session_scope,
    )
    if active != expected:
        return False
    with _active_turn_lock:
        return _active_turn_registry.get((expected[1], expected[2])) == expected[0]


def matches_current_workflow_session(authority: CurrentTurnUserAuthority) -> bool:
    """Require authority to match the independently bound host session."""
    if not matches_active_workflow_turn(authority):
        return False
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if not platform:
        return authority.platform_scope.strip().lower() in {"manual", "cli", "tui"}
    if platform in {
        "background",
        "cron",
        "delegate",
        "review",
        "subagent",
        "webhook",
    }:
        return False
    session_id = get_session_env("HERMES_SESSION_ID", "").strip()
    if not session_id:
        return False
    return hmac.compare_digest(
        authority.platform_scope.strip().lower(), platform
    ) and hmac.compare_digest(authority.session_scope, session_id)


def opaque_workflow_action_id(
    authority: CurrentTurnUserAuthority | None,
) -> str:
    """Return a fresh opaque id with no turn, session, or resource material."""
    del authority
    return f"opaque-action-{secrets.token_hex(16)}"


def clear_current_turn_user_authority() -> None:
    active = _active_workflow_turn.get()
    if active is not None:
        with _active_turn_lock:
            key = (active[1], active[2])
            if _active_turn_registry.get(key) == active[0]:
                _active_turn_registry.pop(key, None)
    _current_turn_user_authority.set(None)
    _active_workflow_turn.set(None)
