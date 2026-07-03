"""Conversational Kanban intake guardrail for gateway messages.

Default-off runtime support for turning assistant-detected card-worthy
conversation scope into a stored pending Kanban card proposal. Short approval
phrases can execute only an exact, source-bound pending proposal.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Protocol

from hermes_constants import get_hermes_home

APPROVAL = "approve"
DENY = "deny"
NONE = "none"
_ALLOWED_STATUSES = {"triage", "blocked"}
_RAW_TRANSCRIPT_KEYS = {
    "raw_transcript",
    "transcript",
    "messages",
    "message_text",
    "raw_message",
    "raw_body",
    "source_ids",
    "chat_id",
    "thread_id",
    "user_id",
    "message_id",
}
_SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:medical|diagnosis|prescription|illness|fever|child|family|wife|spouse)\b", re.I),
    re.compile(r"\b(?:value\s*grill|stance|identity|credential|secret|token|password)\b", re.I),
    re.compile(r"(?:의료|진단|처방|복용|열|해수|아이|자녀|가족|아내|수지|가치관|정체성|비밀번호|토큰|시크릿)"),
)
_ID_LIKE_RE = re.compile(r"\b\d{8,}\b")
_TITLE_MAX_CHARS = 72
CURRENT_POLICY_VERSION = "kanban-intake-policy/v2"
_SAFE_SENSITIVE_TITLE_PHRASES = (
    "child health",
    "childcare condition",
    "family health",
    "condition lifelog capture",
    "child health lifelog capture",
    "childcare lifelog capture",
)
_GENERIC_TITLE_RE = re.compile(
    r"(?:review\s+.*follow[-\s]?up|plan\s+kanban\s+follow[-\s]?up|define\s+next\s+action|safe\s+ops\s+follow[-\s]?up|conversational\s+kanban\s+follow[-\s]?up)",
    re.I,
)
_CASUAL_TITLE_NOISE_RE = re.compile(
    r"(?:ㅋㅋ+|ㅎㅎ+|ㅠㅠ+|너무\s*허접하다|허접하다|왜\s*이래\??|게이트웨이\s*재시작\s*했고|게이트웨이재시작했고)",
    re.I,
)
_EXPLICIT_CARD_REQUEST_RE = re.compile(
    r"(?:카드로\s*남겨|(?:새\s*)?카드(?:로)?\s*(?:만들|생성)(?:어|해|해줘|해주세요|하자|요청)?|새\s*(?:tracking\s*)?카드\s*후보|새\s*tracking\s*card\s*후보|별도\s*카드\s*생성|칸반에\s*추가|add\s+(?:a\s+)?kanban\s+card|create\s+(?:a\s+)?card)",
    re.I,
)
_PROPOSAL_RECORD_REQUEST_RE = re.compile(
    r"(?:카드로\s*남겨|카드\s*후보(?:로)?\s*(?:올려|남겨)|"
    r"새\s*(?:tracking\s*)?카드\s*후보|새\s*tracking\s*card\s*후보|"
    r"별도\s*카드\s*(?:생성|후보|로\s*남겨)|"
    r"(?:이\s*)?내용.*새\s*카드(?:로)?\s*(?:만들|생성)|"
    r"proposal\s+card|tracking\s+card\s+candidate)",
    re.I,
)
_DIRECT_CARD_OPERATION_RE = re.compile(
    r"(?:"
    r"(?:[\w가-힣_.-]+\s*)?보드(?:에|로)?\s*.*카드\s*(?:만들|생성|추가)"
    r"|create\s+(?:a\s+)?card\s+(?:on|in)\s+[\w_.-]+"
    r"|add\s+(?:a\s+)?card\s+(?:to|on|in)\s+[\w_.-]+"
    r")",
    re.I,
)
_KANBAN_CARD_ID_RE = r"\bt_[a-f0-9]{6,}\b"
_EXISTING_CARD_REF_RE = re.compile(
    rf"(?:{_KANBAN_CARD_ID_RE}|기존\s*카드|tracking\s*card|이\s*카드)",
    re.I,
)
_EXISTING_CARD_UPDATE_VERB_RE = re.compile(
    r"(?:업데이트|update|코멘트\s*추가|댓글\s*추가|comment|"
    r"진행\s*상태|진행상태|status|progress|상태\s*기록|기록\s*남겨|"
    r"기록(?:해줘|해|해주세요)?|남겨|완료\s*기록|승인|repo\s*URL|"
    r"PR\s*URL|artifact\s*link|verification\s*summary|handoff\s*note|"
    r"링크\s*기록|URL\s*기록|검증\s*요약|인계\s*노트)",
    re.I,
)
_STANDALONE_PROGRESS_UPDATE_RE = re.compile(
    r"(?:진행\s*상태|진행상태|status|progress)\s*(?:업데이트|update|기록|남겨)",
    re.I,
)
_APPROVED_LIVE_SMOKE_RE = re.compile(
    r"(?:승인|approved).*(?:live\s*smoke|라이브\s*스모크|smoke|스모크).*(?:blocked|차단|블록).*(?:card|카드)",
    re.I,
)
_BOARD_REQUEST_RE = re.compile(r"(?:보드\s*생성|보드\s*만들|칸반보드\s*만들|create\s+(?:a\s+)?board)", re.I)
_KANBAN_META_RE = re.compile(
    r"(?:카드\s*생성\s*조건|조건이\s*너무\s*후|칸반보드가\s*필요한거|쓸데\s*없이|너무\s*후한|카드후보\s*타이틀)",
    re.I,
)
_ONE_OFF_QUESTION_RE = re.compile(r"(?:어떻게\s*하지|왜\s*이래|뭐야\??|맞지\??|아니야\??|낫지\s*않나|\?\?)")
_DURABLE_PROJECT_RE = re.compile(
    # Durable anchors must name a project/system/workstream or implementation
    # object. Generic workflow verbs like "리뷰" are intentionally excluded:
    # "리뷰까지 서브에이전트 시켜서 진행해봐" is a current-turn instruction,
    # not a durable Kanban follow-up.
    r"(?:프로젝트|스프린트|장기|워크스트림|JÖKL|jokl|lifelog|gateway|게이트웨이|kanban\s*intake|칸반\s*intake|title\s*generator|cron|recurring|migration|implementation|구현|테스트|커밋|재발\s*방지)",
    re.I,
)
_CONCRETE_FOLLOWUP_RE = re.compile(
    r"(?:구현|테스트|커밋|리뷰|분석|수정|작성|검증|재발\s*방지|다듬|plan|writing plan|migration|cron|smoke)",
    re.I,
)
_READ_ONLY_ACTION_RE = re.compile(
    r"(?:실행\s*(?:은\s*)?금지|실행하지\s*말|생성\s*금지|만\s*추천|추천\s*목록|확인만|목록만|read[-\s]?only|recommend(?:ation)?\s+only)",
    re.I,
)
_CANDIDATE_AUDIT_RE = re.compile(
    r"(?:(?:보드|카드)\s*(?:또는|/)?\s*(?:카드)?\s*후보|카드\s*후보|board/card\s+candidate|kanban\s+candidate)",
    re.I,
)


@dataclass(frozen=True)
class KanbanIntakeConfig:
    enabled: bool = False
    platforms: tuple[str, ...] = ("discord",)
    default_board: str = ""
    default_assignee: str = "default"
    default_tenant: str = "lifelog"
    default_status: str = "blocked"
    proposal_ttl_seconds: int = 1800
    max_pending_per_session: int = 1
    short_approval_phrases: tuple[str, ...] = ("승인", "ㅇㅇ", "고고", "그렇게 해", "좋아", "진행")
    deny_phrases: tuple[str, ...] = ("취소", "ㄴㄴ", "하지마", "보류")
    detector: str = "heuristic"
    auxiliary_detector_enabled: bool = False
    redact_before_auxiliary: bool = True
    title_generator_enabled: bool = False
    title_generator_mode: str = "fallback_only"
    title_generator_timeout_seconds: int = 3
    pending_retention_seconds: int = 86400
    card_body_include_raw_source_ids: bool = False
    max_body_chars: int = 4000
    store_path: Optional[Path] = None

    @property
    def normalized_platforms(self) -> set[str]:
        return {str(p).lower() for p in self.platforms if str(p).strip()}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _bounded_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 30 * 86400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < minimum:
        return default
    return min(parsed, maximum)


def parse_config(config: Optional[dict[str, Any]]) -> KanbanIntakeConfig:
    """Parse ``kanban.conversational_intake`` from config.yaml-shaped data."""
    root = config or {}
    block = ((root.get("kanban") or {}).get("conversational_intake") or {})
    if not isinstance(block, dict):
        block = {}

    status = str(block.get("default_status") or "blocked").strip().lower()
    if status not in _ALLOWED_STATUSES:
        status = "blocked"

    platforms_raw = block.get("platforms", ("discord",))
    if isinstance(platforms_raw, str):
        platforms = tuple(p.strip().lower() for p in platforms_raw.split(",") if p.strip())
    elif isinstance(platforms_raw, Iterable):
        platforms = tuple(str(p).strip().lower() for p in platforms_raw if str(p).strip())
    else:
        platforms = ("discord",)

    def phrases(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = block.get(key, default)
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, Iterable):
            items = list(raw)
        else:
            items = list(default)
        cleaned = tuple(str(item).strip() for item in items if str(item).strip())
        return cleaned or default

    store_path = block.get("store_path")
    path_obj = Path(store_path).expanduser() if store_path else None
    title_generator_mode = str(block.get("title_generator_mode") or "fallback_only").strip().lower() or "fallback_only"
    title_generator_enabled = _as_bool(block.get("title_generator_enabled"), False)
    if title_generator_mode not in {"fallback_only", "constrained_llm"}:
        title_generator_mode = "fallback_only"
        title_generator_enabled = False
    if title_generator_mode == "fallback_only":
        title_generator_enabled = False
    return KanbanIntakeConfig(
        enabled=_as_bool(block.get("enabled"), False),
        platforms=platforms or ("discord",),
        default_board=str(block.get("default_board") or "").strip(),
        default_assignee=str(block.get("default_assignee") or "default").strip() or "default",
        default_tenant=str(block.get("default_tenant") or "lifelog").strip() or "lifelog",
        default_status=status,
        proposal_ttl_seconds=_bounded_int(block.get("proposal_ttl_seconds"), 1800, minimum=60, maximum=86400),
        max_pending_per_session=_bounded_int(block.get("max_pending_per_session"), 1, minimum=1, maximum=10),
        short_approval_phrases=phrases("short_approval_phrases", KanbanIntakeConfig.short_approval_phrases),
        deny_phrases=phrases("deny_phrases", KanbanIntakeConfig.deny_phrases),
        detector=str(block.get("detector") or "heuristic").strip().lower() or "heuristic",
        auxiliary_detector_enabled=_as_bool(block.get("auxiliary_detector_enabled"), False),
        redact_before_auxiliary=_as_bool(block.get("redact_before_auxiliary"), True),
        title_generator_enabled=title_generator_enabled,
        title_generator_mode=title_generator_mode,
        title_generator_timeout_seconds=_bounded_int(
            block.get("title_generator_timeout_seconds"), 3, minimum=1, maximum=5
        ),
        pending_retention_seconds=_bounded_int(block.get("pending_retention_seconds"), 86400, minimum=3600, maximum=30 * 86400),
        card_body_include_raw_source_ids=_as_bool(block.get("card_body_include_raw_source_ids"), False),
        max_body_chars=_bounded_int(block.get("max_body_chars"), 4000, minimum=500, maximum=16000),
        store_path=path_obj,
    )


@dataclass(frozen=True)
class SourceBinding:
    platform: str
    chat_id: str
    thread_id: Optional[str]
    user_id: str
    session_key: str
    message_id: Optional[str] = None

    @classmethod
    def from_source(cls, source: Any, session_key: str, *, message_id: Optional[str] = None) -> "SourceBinding":
        platform_obj = getattr(source, "platform", "")
        platform = getattr(platform_obj, "value", platform_obj)
        user_id = getattr(source, "user_id", None)
        if not user_id:
            raise ValueError("user_id is required for executable kanban intake approvals")
        return cls(
            platform=str(platform or ""),
            chat_id=str(getattr(source, "chat_id", "") or ""),
            thread_id=(str(getattr(source, "thread_id", "")) or None),
            user_id=str(user_id),
            session_key=str(session_key or ""),
            message_id=str(message_id) if message_id is not None else None,
        )


@dataclass
class KanbanCardProposal:
    board: str
    title: str
    body: dict[str, Any]
    source_ref: str
    user_id: str
    proposed_status: str = "blocked"
    domain: str = "lifelog-core"
    tenant: str = "lifelog"
    assignee: str = "default"
    priority: int = 0
    why: str = "multi-step follow-up work"
    idempotency_key: Optional[str] = None

    def normalized(self, cfg: KanbanIntakeConfig) -> "KanbanCardProposal":
        self.board = (self.board or cfg.default_board).strip()
        self.proposed_status = (self.proposed_status or cfg.default_status or "blocked").strip().lower()
        self.tenant = (self.tenant or cfg.default_tenant).strip()
        self.assignee = (self.assignee or cfg.default_assignee).strip()
        if not self.idempotency_key:
            self.idempotency_key = f"kanban-intake:{self.source_ref}:{self.title.strip()}"
        return self


@dataclass
class PendingKanbanApproval:
    pending_id: str
    binding: SourceBinding
    proposal: KanbanCardProposal
    created_at: float
    expires_at: float
    status: str = "pending"
    source_ids: dict[str, Any] = field(default_factory=dict)
    purge_after: float = 0.0
    policy_version: str = CURRENT_POLICY_VERSION


@dataclass(frozen=True)
class ActiveLookup:
    state: Literal["none", "one", "ambiguous"]
    pending: Optional[PendingKanbanApproval] = None
    count: int = 0


@dataclass(frozen=True)
class ApprovalResult:
    handled: bool
    message: str = ""
    task_id: Optional[str] = None
    verified: bool = False
    action: str = NONE


@dataclass(frozen=True)
class IntakeDetectionRequest:
    platform: str
    session_key: str
    source_ref: str
    user_summary: str
    assistant_summary: str
    default_board: str
    default_tenant: str


@dataclass(frozen=True)
class DetectorDecision:
    card_worthy: bool
    title: str = ""
    domain: str = "lifelog-core"
    tenant: str = "lifelog"
    proposed_status: str = "blocked"
    why: str = ""
    body: dict[str, Any] = field(default_factory=dict)
    candidate_class: str = "insufficient_signal"


@dataclass(frozen=True)
class TitleGenerationRule:
    allowed_verbs: tuple[str, ...] = ("Review", "Fix", "Verify", "Investigate", "Record", "Implement")
    allowed_objects: tuple[str, ...] = (
        "medication intake",
        "medication reminder",
        "sleep log",
        "condition",
        "diet intake",
        "childcare",
        "training",
        "travel",
        "title generation",
        "gateway",
        "kanban intake",
    )
    max_chars: int = _TITLE_MAX_CHARS
    min_words: int = 3
    max_words: int = 10


@dataclass(frozen=True)
class CandidateTitleDraft:
    title: str
    action: str
    object: str
    rationale: str = ""


@dataclass(frozen=True)
class ProposalEligibility:
    eligible: bool
    reason: str
    matched_rule: str = ""
    candidate_class: str = "insufficient_signal"


@dataclass(frozen=True)
class TitleQualityResult:
    passed: bool
    reason_codes: tuple[str, ...] = ()


class KanbanIntakeDetector(Protocol):
    def detect(self, request: IntakeDetectionRequest) -> DetectorDecision: ...


def classify_reply(text: str, cfg: KanbanIntakeConfig) -> str:
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized or normalized.startswith("/"):
        return NONE
    approvals = {" ".join(p.lower().split()) for p in cfg.short_approval_phrases}
    denies = {" ".join(p.lower().split()) for p in cfg.deny_phrases}
    if normalized in approvals:
        return APPROVAL
    if normalized in denies:
        return DENY
    return NONE


def _contains_sensitive_payload(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    # Safe semantic titles may name the domain class (for example "child
    # health") without leaking raw person names, symptoms, measurements, or
    # transcript details. Remove those allowlisted phrases before applying the
    # broad privacy guard used for raw payloads.
    screened = value.lower()
    for phrase in _SAFE_SENSITIVE_TITLE_PHRASES:
        screened = screened.replace(phrase.lower(), "")
    return any(p.search(screened) for p in _SENSITIVE_PATTERNS)


def _iter_body_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_body_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_body_strings(item)


def validate_proposal(proposal: KanbanCardProposal, cfg: KanbanIntakeConfig) -> tuple[bool, str]:
    proposal.normalized(cfg)
    if not proposal.board:
        return False, "missing board"
    if proposal.proposed_status not in _ALLOWED_STATUSES:
        return False, f"unsupported status {proposal.proposed_status!r}"
    if not proposal.title.strip():
        return False, "missing title"
    title_quality = evaluate_title_quality(proposal.title)
    if not title_quality.passed:
        return False, "title quality failed: " + ",".join(title_quality.reason_codes)
    if not proposal.user_id:
        return False, "missing user binding"
    if _contains_sensitive_payload(proposal.title) or _contains_sensitive_payload(proposal.why):
        return False, "sensitive payload in title/why"
    if not isinstance(proposal.body, dict):
        return False, "body must be an object"
    if any(str(key) in _RAW_TRANSCRIPT_KEYS for key in proposal.body.keys()):
        return False, "raw transcript/source field in body"
    body_json = json.dumps(proposal.body, ensure_ascii=False, sort_keys=True)
    if len(body_json) > cfg.max_body_chars:
        return False, "body too long"
    if not cfg.card_body_include_raw_source_ids and _ID_LIKE_RE.search(body_json.replace(proposal.source_ref, "")):
        return False, "raw numeric source id in body"
    for value in _iter_body_strings(proposal.body):
        if _contains_sensitive_payload(value):
            return False, "sensitive payload in body"
    lowered = body_json.lower()
    if any(token in lowered for token in ("dispatch=true", "status=ready", "status=running", '"status": "ready"', '"status": "running"')):
        return False, "unsafe dispatch/status flag in body"
    return True, "ok"


def _safe_contract_body(proposal: KanbanCardProposal) -> str:
    body = dict(proposal.body or {})
    contract = {
        "source_ref": proposal.source_ref,
        "domain": proposal.domain,
        "tenant": proposal.tenant,
        "status": proposal.proposed_status,
        "why": proposal.why,
        "contract": body,
        "safety": {
            "dispatch": False,
            "ready_or_running": False,
            "live_db_cron_graphify_public_mutation": False,
        },
    }
    return json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2)


def render_proposal_message(proposal: KanbanCardProposal) -> str:
    status_label = "blocked review" if proposal.proposed_status == "blocked" else proposal.proposed_status
    return (
        f"카드 후보 감지: 이건 Kanban에 {status_label} 카드로 남기는 게 좋다.\n"
        f"- board: {proposal.board}\n"
        f"- title: {proposal.title}\n"
        f"- domain: {proposal.domain}\n"
        f"- tenant: {proposal.tenant}\n"
        f"- status: {proposal.proposed_status}\n"
        f"- why: {proposal.why}\n"
        "- safety: dispatch 없음, ready/running 아님, live DB/cron/Graphify/JÖKL public mutation 없음\n\n"
        "승인하려면 “승인/ㅇㅇ/고고”, 취소하려면 “취소”."
    )


class PendingKanbanStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (get_hermes_home() / "kanban" / "intake_pending.db")

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        self._migrate(conn)
        return conn

    def connect_readonly(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(kanban_intake_pending)").fetchall()}
        if "policy_version" not in columns:
            conn.execute(
                "ALTER TABLE kanban_intake_pending ADD COLUMN policy_version TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "UPDATE kanban_intake_pending SET policy_version = '' WHERE policy_version IS NULL"
            )
        conn.commit()

    def put_pending(
        self,
        proposal: KanbanCardProposal,
        binding: SourceBinding,
        cfg: KanbanIntakeConfig,
        *,
        now: Optional[float] = None,
        source_ids: Optional[dict[str, Any]] = None,
    ) -> PendingKanbanApproval:
        if not binding.user_id:
            raise ValueError("user_id is required")
        now = time.time() if now is None else float(now)
        proposal.user_id = binding.user_id
        proposal.normalized(cfg)
        ok, reason = validate_proposal(proposal, cfg)
        if not ok:
            raise ValueError(reason)
        pending = PendingKanbanApproval(
            pending_id="kp_" + uuid.uuid4().hex[:16],
            binding=binding,
            proposal=proposal,
            created_at=now,
            expires_at=now + cfg.proposal_ttl_seconds,
            status="pending",
            source_ids=dict(source_ids or {}),
            purge_after=now + cfg.pending_retention_seconds,
        )
        with self.connect() as conn:
            active_count = conn.execute(
                """
                SELECT COUNT(*) AS n FROM kanban_intake_pending
                WHERE status = 'pending'
                  AND platform = ? AND chat_id = ? AND COALESCE(thread_id, '') = COALESCE(?, '')
                  AND user_id = ? AND session_key = ? AND expires_at > ?
                """,
                (binding.platform, binding.chat_id, binding.thread_id, binding.user_id, binding.session_key, now),
            ).fetchone()["n"]
            if int(active_count or 0) >= cfg.max_pending_per_session:
                raise ValueError("active pending proposal limit exceeded")
            conn.execute(
                """
                INSERT INTO kanban_intake_pending (
                  pending_id, created_at, expires_at, status,
                  platform, chat_id, thread_id, user_id, session_key,
                  source_ref, source_ids_json, board, title, body_json,
                  domain, tenant, assignee, priority, proposed_status,
                  why, idempotency_key, redaction_version, policy_version, purge_after, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.pending_id, pending.created_at, pending.expires_at, pending.status,
                    binding.platform, binding.chat_id, binding.thread_id, binding.user_id, binding.session_key,
                    proposal.source_ref, json.dumps(pending.source_ids, ensure_ascii=False, sort_keys=True),
                    proposal.board, proposal.title, json.dumps(proposal.body, ensure_ascii=False, sort_keys=True),
                    proposal.domain, proposal.tenant, proposal.assignee, int(proposal.priority), proposal.proposed_status,
                    proposal.why, proposal.idempotency_key, "kanban-intake-redaction/v1", CURRENT_POLICY_VERSION, pending.purge_after, now,
                ),
            )
            conn.commit()
        return pending

    def get_active_for_source(self, binding: SourceBinding, *, now: Optional[float] = None) -> ActiveLookup:
        now = time.time() if now is None else float(now)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM kanban_intake_pending
                WHERE status = 'pending'
                  AND platform = ? AND chat_id = ? AND COALESCE(thread_id, '') = COALESCE(?, '')
                  AND user_id = ? AND session_key = ? AND expires_at > ?
                ORDER BY created_at DESC
                """,
                (binding.platform, binding.chat_id, binding.thread_id, binding.user_id, binding.session_key, now),
            ).fetchall()
        if not rows:
            return ActiveLookup("none", None, 0)
        if len(rows) > 1:
            return ActiveLookup("ambiguous", None, len(rows))
        return ActiveLookup("one", self._from_row(rows[0]), 1)

    def mark_status(self, pending_id: str, status: str, *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else float(now)
        with self.connect() as conn:
            conn.execute(
                "UPDATE kanban_intake_pending SET status = ?, updated_at = ? WHERE pending_id = ?",
                (status, now, pending_id),
            )
            conn.commit()

    def purge(self, *, now: Optional[float] = None) -> int:
        now = time.time() if now is None else float(now)
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM kanban_intake_pending WHERE purge_after <= ? OR (status = 'pending' AND expires_at <= ?)",
                (now, now),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def review_pending(self, *, now: Optional[float] = None, include_all: bool = False, limit: int = 100) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        status_clause = "" if include_all else "WHERE status = 'pending'"
        conn = self.connect_readonly()
        if conn is None:
            return {"current_policy_version": CURRENT_POLICY_VERSION, "counts": {}, "items": []}
        with conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'kanban_intake_pending'"
            ).fetchone()
            if table is None:
                return {"current_policy_version": CURRENT_POLICY_VERSION, "counts": {}, "items": []}
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(kanban_intake_pending)").fetchall()}
            policy_expr = "policy_version" if "policy_version" in columns else "'' AS policy_version"
            rows = conn.execute(
                f"""
                SELECT pending_id, status, title, created_at, expires_at,
                       {policy_expr}, board, tenant
                FROM kanban_intake_pending
                {status_clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        items: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for row in rows:
            policy_version = row["policy_version"] or ""
            raw_title = row["title"] or ""
            title_quality = evaluate_title_quality(raw_title)
            title = minimize_for_detector(raw_title, max_chars=120)
            flags = []
            if policy_version != CURRENT_POLICY_VERSION:
                flags.append("stale_policy")
            if row["status"] == "pending" and float(row["expires_at"] or 0) <= now:
                flags.append("expired")
            flags.extend(title_quality.reason_codes)
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            items.append({
                "pending_id": row["pending_id"],
                "status": row["status"],
                "title": title,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "policy_version": policy_version,
                "current_policy_version": CURRENT_POLICY_VERSION,
                "board": row["board"],
                "tenant": row["tenant"],
                "flags": tuple(dict.fromkeys(flags)),
            })
        return {"current_policy_version": CURRENT_POLICY_VERSION, "counts": counts, "items": items}

    def revalidate_pending(self, *, now: Optional[float] = None, include_all: bool = False, limit: int = 100) -> dict[str, Any]:
        review = self.review_pending(now=now, include_all=include_all, limit=limit)
        for item in review["items"]:
            flags = set(item["flags"])
            item["would_pass"] = not (flags & {"stale_policy", "expired", "generic_title", "raw_user_copy", "sensitive_leak", "empty"})
            item["would_invalidate"] = not item["would_pass"]
        return review

    def bulk_invalidate(self, *, where: str = "stale-or-generic", dry_run: bool = True, now: Optional[float] = None) -> dict[str, Any]:
        review = self.revalidate_pending(now=now, include_all=False, limit=10000)
        if where != "stale-or-generic":
            raise ValueError("unsupported where filter")
        targets = [item["pending_id"] for item in review["items"] if item.get("would_invalidate")]
        updated = 0
        if not dry_run and targets:
            timestamp = time.time() if now is None else float(now)
            with self.connect() as conn:
                for pending_id in targets:
                    cur = conn.execute(
                        """
                        UPDATE kanban_intake_pending
                        SET status = 'invalid', updated_at = ?
                        WHERE pending_id = ? AND status = 'pending'
                        """,
                        (timestamp, pending_id),
                    )
                    updated += int(cur.rowcount or 0)
                conn.commit()
        return {"dry_run": dry_run, "where": where, "matched": len(targets), "updated": updated, "pending_ids": targets[:25]}

    def _from_row(self, row: sqlite3.Row) -> PendingKanbanApproval:
        binding = SourceBinding(
            platform=row["platform"], chat_id=row["chat_id"], thread_id=row["thread_id"],
            user_id=row["user_id"], session_key=row["session_key"],
        )
        proposal = KanbanCardProposal(
            board=row["board"], title=row["title"],
            body=json.loads(row["body_json"] or "{}"), source_ref=row["source_ref"],
            user_id=row["user_id"], proposed_status=row["proposed_status"],
            domain=row["domain"] or "lifelog-core", tenant=row["tenant"] or "lifelog",
            assignee=row["assignee"] or "default", priority=int(row["priority"] or 0),
            why=row["why"] or "", idempotency_key=row["idempotency_key"],
        )
        try:
            source_ids = json.loads(row["source_ids_json"] or "{}")
        except json.JSONDecodeError:
            source_ids = {}
        return PendingKanbanApproval(
            pending_id=row["pending_id"], binding=binding, proposal=proposal,
            created_at=float(row["created_at"]), expires_at=float(row["expires_at"]),
            status=row["status"], source_ids=source_ids, purge_after=float(row["purge_after"]),
            policy_version=(row["policy_version"] or ""),
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_intake_pending (
  pending_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL,
  platform TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  thread_id TEXT,
  user_id TEXT NOT NULL,
  session_key TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  source_ids_json TEXT NOT NULL,
  board TEXT NOT NULL,
  title TEXT NOT NULL,
  body_json TEXT NOT NULL,
  domain TEXT,
  tenant TEXT,
  assignee TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  proposed_status TEXT NOT NULL,
  why TEXT,
  idempotency_key TEXT,
  redaction_version TEXT NOT NULL,
  policy_version TEXT NOT NULL DEFAULT '',
  purge_after REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kanban_intake_lookup
ON kanban_intake_pending(platform, chat_id, thread_id, user_id, session_key, status, expires_at);
"""


def execute_pending_approval(pending: PendingKanbanApproval, cfg: KanbanIntakeConfig) -> ApprovalResult:
    proposal = pending.proposal.normalized(cfg)
    ok, reason = validate_proposal(proposal, cfg)
    if not ok:
        return ApprovalResult(True, f"Kanban 카드 생성 차단: {reason}", action=APPROVAL)
    from hermes_cli import kanban_db as kb

    conn = kb.connect(board=proposal.board)
    try:
        kwargs: dict[str, Any] = {
            "title": proposal.title,
            "body": _safe_contract_body(proposal),
            "assignee": proposal.assignee,
            "tenant": proposal.tenant,
            "priority": int(proposal.priority),
            "created_by": "kanban-intake",
            "idempotency_key": proposal.idempotency_key,
            "board": proposal.board,
            "session_id": pending.binding.session_key,
        }
        if proposal.proposed_status == "blocked":
            kwargs["initial_status"] = "blocked"
        else:
            kwargs["triage"] = True
        task_id = kb.create_task(conn, **kwargs)
        task = kb.get_task(conn, task_id)
        verified = bool(task and task.status == proposal.proposed_status and task.title == proposal.title)
    finally:
        conn.close()
    if not verified:
        return ApprovalResult(True, f"Kanban 카드 생성 후 검증 실패: {task_id}", task_id=task_id, verified=False, action=APPROVAL)
    return ApprovalResult(True, f"Kanban {proposal.proposed_status} 카드 생성/검증 완료: {task_id}", task_id=task_id, verified=True, action=APPROVAL)


def handle_reply(
    text: str,
    binding: SourceBinding,
    cfg: KanbanIntakeConfig,
    store: PendingKanbanStore,
) -> ApprovalResult:
    action = classify_reply(text, cfg)
    if action == NONE:
        return ApprovalResult(False)
    lookup = store.get_active_for_source(binding)
    if lookup.state == "none":
        return ApprovalResult(False)
    if lookup.state == "ambiguous":
        return ApprovalResult(True, "Kanban 카드 후보가 여러 개라서 실행하지 않았다. 하나만 남기고 다시 승인해줘.", action=action)
    assert lookup.pending is not None
    if action == DENY:
        store.mark_status(lookup.pending.pending_id, "denied")
        return ApprovalResult(True, "Kanban 카드 후보 취소 완료.", action=DENY)
    if lookup.pending.policy_version != CURRENT_POLICY_VERSION:
        store.mark_status(lookup.pending.pending_id, "needs_revalidation")
        return ApprovalResult(
            True,
            "Kanban 카드 후보 정책 버전이 오래되어 실행하지 않았다. revalidate 또는 새 후보 생성이 필요하다.",
            action=APPROVAL,
        )
    result = execute_pending_approval(lookup.pending, cfg)
    store.mark_status(lookup.pending.pending_id, "executed" if result.verified else "invalid")
    return result


def minimize_for_detector(text: str, *, max_chars: int = 500) -> str:
    value = str(text or "")
    value = _ID_LIKE_RE.sub("[id]", value)
    typed_redactions = (
        (re.compile(r"(?:해수|아이|자녀|\bchild\b|\bchildcare\b)", re.I), "[child-sensitive]"),
        (re.compile(r"(?:가족|아내|수지|\bfamily\b|\bwife\b|\bspouse\b)", re.I), "[family-sensitive]"),
        (re.compile(r"(?:의료|진단|처방|복용|열|\bmedical\b|\bdiagnosis\b|\bprescription\b|\billness\b|\bfever\b)", re.I), "[health-sensitive]"),
        (re.compile(r"(?:비밀번호|토큰|시크릿|\bcredential\b|\bsecret\b|\btoken\b|\bpassword\b)", re.I), "[security-sensitive]"),
        (re.compile(r"(?:가치관|정체성|\bvalue\s*grill\b|\bstance\b|\bidentity\b)", re.I), "[private-sensitive]"),
    )
    for pat, marker in typed_redactions:
        value = pat.sub(marker, value)
    value = " ".join(value.split())
    if len(value) > max_chars:
        value = value[: max_chars - 1] + "…"
    return value


def _strip_chat_speaker_prefixes(value: str) -> str:
    """Remove gateway/chat display-name prefixes from generated titles.

    Multi-user Discord sessions show human messages to the model with labels like
    ``[상현]``. Auxiliary title generators sometimes copy that label verbatim.
    Titles are board artifacts, so speaker labels are noise and must never be
    persisted or rendered as card titles.
    """
    text = str(value or "")
    # Strip one or more short leading bracket labels, but do not remove bracketed
    # content in the middle of a legitimate title.
    return re.sub(r"^(?:\s*\[[^\]\n]{1,32}\]\s*)+", "", text).strip()


def _compact_title(value: str, *, max_chars: int = _TITLE_MAX_CHARS) -> str:
    title = _strip_chat_speaker_prefixes(str(value or "").replace("\n", " "))
    title = " ".join(title.split())
    title = _ID_LIKE_RE.sub("[id]", title).strip(" -:;,.，。")
    if len(title) > max_chars:
        title = title[: max_chars - 1].rstrip(" -:;,.，。") + "…"
    return title


def _is_generic_title(value: str) -> bool:
    title = _compact_title(value)
    if not title:
        return True
    return bool(_GENERIC_TITLE_RE.search(title))


def _has_any(text: str, *needles: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    return any(str(needle).lower() in lowered or str(needle) in value for needle in needles)


def _is_clunky_title(value: str) -> bool:
    title = _compact_title(value)
    if not title:
        return True
    return _has_any(
        title,
        "[상현]",
        "이거",
        "왜이래",
        "왜 이래",
        "낫지 않나",
        "어떻게하지",
        "기준이 뭐야",
        "??",
        "ㅅㅂ",
        "ㅋㅋ",
    )


def _title_compare_key(value: str) -> str:
    """Return a compact key for raw-title copy detection.

    This ignores whitespace/punctuation/case so a generator cannot evade the
    guard by trimming or lightly reformatting a user utterance.
    """
    text = _strip_chat_speaker_prefixes(str(value or "")).lower()
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _longest_common_substring_len(left: str, right: str) -> int:
    if not left or not right:
        return 0
    if len(left) > len(right):
        left, right = right, left
    previous = [0] * (len(right) + 1)
    best = 0
    for left_ch in left:
        current = [0]
        for idx, right_ch in enumerate(right, start=1):
            if left_ch == right_ch:
                value = previous[idx - 1] + 1
                best = max(best, value)
                current.append(value)
            else:
                current.append(0)
        previous = current
    return best


def _looks_like_raw_user_title(title: str, request: IntakeDetectionRequest) -> bool:
    """Return True when a title is copied from the current user message.

    Generated card titles may share a short domain phrase with the user's text
    (for example "title generator"), but they must not be the user utterance,
    a trimmed speaker-prefixed line, or a long substring of it.
    """
    title_key = _title_compare_key(title)
    user_key = _title_compare_key(request.user_summary)
    if len(title_key) < 8 or len(user_key) < 8:
        return False
    if title_key in user_key or user_key in title_key:
        return True
    overlap = _longest_common_substring_len(title_key, user_key)
    return overlap >= max(18, int(len(title_key) * 0.85))


def evaluate_title_quality(title: str, request: Optional[IntakeDetectionRequest] = None) -> TitleQualityResult:
    """Score the deterministic card-title rubric: Verb + Object + Outcome/scope.

    The helper is intentionally small and stdlib-only so tests, no-live smoke,
    and operator diagnostics can share one definition of generic/raw/sensitive
    title failures.
    """
    compact = _compact_title(title)
    reasons: list[str] = []
    if not compact:
        reasons.append("empty")
    if _is_generic_title(compact):
        reasons.append("generic_title")
    if _is_clunky_title(compact):
        reasons.append("clunky_title")
    if _contains_sensitive_payload(compact):
        reasons.append("sensitive_leak")
    if request is not None and _looks_like_raw_user_title(compact, request):
        reasons.append("raw_user_copy")
    words = compact.split()
    allowed_verbs = ("Review", "Fix", "Verify", "Investigate", "Record", "Implement", "Improve", "Add", "Plan", "Create")
    if not any(compact == verb or compact.startswith(f"{verb} ") for verb in allowed_verbs):
        reasons.append("missing_action_verb")
    if len(words) < 3:
        reasons.append("missing_object_or_scope")
    # Require something after the object: scope/outcome nouns, domain anchors,
    # or an accepted three-word domain title such as "Improve conversational
    # Kanban intake". This blocks "Review follow-up" while keeping current
    # concise project titles valid.
    if len(words) >= 3 and not any(
        anchor.lower() in compact.lower()
        for anchor in (
            "regression", "tests", "capture", "scope", "suppression", "generator",
            "quality", "normalization", "workflow", "backlog", "cards", "intake",
            "follow-up", "lifelog-control", "kanban", "gateway", "smoke",
        )
    ):
        reasons.append("missing_outcome_scope")
    return TitleQualityResult(not reasons, tuple(dict.fromkeys(reasons)))


def _normalize_kanban_intake_title_intent(text: str) -> str:
    lowered = str(text or "").lower()
    if "kanban" not in lowered and "칸반" not in str(text or ""):
        return ""
    if any(term in lowered for term in ("false positive", "false-positive", "오탐")):
        if any(term in lowered for term in ("test", "regression", "회귀", "테스트")):
            return "Add Kanban intake false-positive regression tests"
        return "Fix Kanban intake false-positive suppression"
    if ("quality" in lowered or "품질" in str(text or "")) and ("title" in lowered or "타이틀" in str(text or "")):
        return "Review Kanban candidate/title quality"
    if "candidate" in lowered and ("title" in lowered or "quality" in lowered):
        return "Review Kanban candidate/title quality"
    if "title generator" in lowered or "타이틀" in str(text or "") or "제목" in str(text or ""):
        return "Improve Kanban intake title generator"
    if any(term in lowered for term in ("test", "regression", "회귀", "테스트")):
        return "Add Kanban intake regression tests"
    if "conversational intake" in lowered:
        return "Improve conversational Kanban intake"
    if "intake" in lowered:
        return "Fix Kanban intake classification quality"
    return ""


def _normalize_project_title_intent(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    if "jÖkl" in value or "JÖKL" in value or "jokl" in lowered:
        if "marketing" in lowered or "마케팅" in value:
            if "packet" in lowered or "패킷" in value:
                return "Plan JÖKL marketing packet generator sprint work"
            return "Plan JÖKL marketing sprint work"
        return "Plan JÖKL project follow-up work"
    return ""


def _normalize_child_family_health_title_intent(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    child_health = any(token in value for token in ("해수", "아이", "자녀", "[child-sensitive]")) or any(
        token in lowered for token in ("child", "childcare")
    )
    health_signal = any(token in value for token in ("열", "증상", "컨디션", "기록", "후속", "[health-sensitive]")) or any(
        token in lowered for token in ("health", "condition", "illness", "record", "follow-up")
    )
    if child_health and health_signal:
        return "Review child health Lifelog capture"
    if ("family" in lowered or "가족" in value) and health_signal:
        return "Review family health Lifelog capture"
    return ""


def _normalize_korean_title_intent(text: str) -> str:
    if _has_any(text, "원문 복사", "raw copy", "raw-copy") and _has_any(text, "타이틀", "title", "제목"):
        return "Fix Kanban title raw-copy guardrail scope"
    if _has_any(text, "타이틀", "title") and _has_any(text, "왜이래", "왜 이래", "변한게 없어", "카드후보"):
        return "Improve Kanban intake title normalization"
    if _has_any(text, "후보", "candidate") and _has_any(text, "보드", "카드", "kanban") and _has_any(text, "Hermes", "헤르메스"):
        return "Review Hermes Kanban board/card candidates"
    if _has_any(text, "해커톤", "hackathon") and _has_any(text, "칸반", "kanban") and _has_any(text, "카드", "board", "보드"):
        return "Create hackathon Kanban board backlog"
    if _has_any(text, "기존 카드", "기존 카드들") and _has_any(text, "기준", "지우", "어떻게"):
        return "Review existing lifelog-control Kanban cards"
    return ""


def _normalize_sensitive_ops_title_intent(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    if any(token in value for token in ("[security-sensitive]", "비밀번호", "토큰", "시크릿")) or any(
        token in lowered for token in ("credential", "secret", "token", "password")
    ):
        return "Review security rotation scope"
    if any(token in value for token in ("[private-sensitive]", "가치관", "정체성")) or any(
        token in lowered for token in ("value grill", "stance", "identity")
    ):
        return "Review private context follow-up scope"
    return ""



def _normalize_lifelog_title_intent(text: str) -> str:
    if not _has_any(text, "lifelog", "라이프로그", "기록", "record", "follow-up", "후속", "[child-sensitive]", "[health-sensitive]", "[family-sensitive]"):
        return ""
    child_health = _normalize_child_family_health_title_intent(text)
    if child_health:
        return child_health
    if _has_any(text, "generic title", "제목", "타이틀", "review lifelog follow-up work 같은") and _has_any(
        text,
        "lifelog",
        "라이프로그",
        "카드후보",
        "카드 후보",
        "candidate",
    ):
        return "Fix Kanban candidate title generation for Lifelog records"
    if _has_any(text, "medication reminder", "복약 리마인더", "약 리마인더", "cron") and _has_any(
        text,
        "누락",
        "missing",
        "regression",
        "재발",
    ):
        return "Fix Lifelog medication reminder cron regression"
    if _has_any(text, "medication", "복약", "약 먹", "약먹", "skipped dose", "복용"):
        return "Review medication intake Lifelog capture"
    if _has_any(text, "sleep", "수면", "취침", "기상"):
        return "Review sleep log Lifelog capture"
    if _has_any(text, "condition", "컨디션", "pain", "통증", "피로", "soreness", "fever", "illness", "증상"):
        return "Review condition Lifelog capture"
    if _has_any(text, "diet", "meal", "식단", "식사", "간식", "먹었"):
        return "Review diet intake Lifelog capture"
    if _has_any(text, "childcare", "육아", "아이", "자녀", "child health"):
        return "Review childcare Lifelog capture"
    if _has_any(text, "training", "exercise", "운동", "주짓수", "레슬링", "mma"):
        return "Review training Lifelog capture"
    if _has_any(text, "travel", "여행", "출장"):
        return "Review travel Lifelog capture"
    return ""


def _raw_title_fallback(request: IntakeDetectionRequest) -> str:
    text = _clean_gate_text(request)
    lowered = text.lower()
    project_normalized = _normalize_project_title_intent(text)
    if project_normalized:
        return project_normalized
    kanban_normalized = _normalize_kanban_intake_title_intent(text)
    if kanban_normalized:
        return kanban_normalized
    child_health = _normalize_child_family_health_title_intent(text)
    if child_health:
        return child_health
    lifelog_normalized = _normalize_lifelog_title_intent(text)
    if lifelog_normalized:
        return lifelog_normalized
    sensitive_ops = _normalize_sensitive_ops_title_intent(text)
    if sensitive_ops:
        return sensitive_ops
    if _has_any(text, "타이틀", "title", "제목") and _has_any(text, "칸반", "kanban", "카드후보", "카드 후보"):
        return "Improve Kanban intake title generator"
    if "live smoke" in lowered and ("lifelog-control" in lowered or "discord" in lowered):
        return "Verify Discord live smoke for lifelog-control"
    if _has_any(text, "gateway", "게이트웨이") and _has_any(text, "구현", "implement"):
        return "Implement gateway follow-up work"
    if _has_any(text, "gateway", "게이트웨이"):
        return "Verify gateway follow-up"
    if _has_any(text, "lifelog", "라이프로그"):
        return "Review Lifelog record capture workflow"
    if _has_any(text, "칸반", "kanban", "카드"):
        return "Plan Kanban follow-up work"
    return "Follow up on requested work"


_SAFE_TITLE_OBJECT_ALIASES = {
    "medication": "medication intake",
    "medication log": "medication intake",
    "medication capture": "medication intake",
    "dose": "medication intake",
    "reminder": "medication reminder",
    "medication cron": "medication reminder",
    "sleep": "sleep log",
    "sleep capture": "sleep log",
    "diet": "diet intake",
    "meal": "diet intake",
    "meal intake": "diet intake",
    "child health": "childcare",
    "childcare record": "childcare",
    "exercise": "training",
    "training log": "training",
    "trip": "travel",
    "travel log": "travel",
    "title generator": "title generation",
    "kanban title generation": "title generation",
    "kanban title generator": "title generation",
    "conversational intake": "kanban intake",
}


def _title_generator_request(request: IntakeDetectionRequest) -> IntakeDetectionRequest:
    """Return the minimized request shape passed to optional title generators."""
    return IntakeDetectionRequest(
        platform=request.platform,
        session_key=request.session_key,
        source_ref=request.source_ref,
        user_summary=minimize_for_detector(request.user_summary, max_chars=280),
        assistant_summary=minimize_for_detector(request.assistant_summary, max_chars=280),
        default_board=request.default_board,
        default_tenant=request.default_tenant,
    )


def _safe_title_object(value: str, rule: TitleGenerationRule) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    if not normalized:
        return ""
    alias = _SAFE_TITLE_OBJECT_ALIASES.get(normalized)
    if alias:
        normalized = alias
    for allowed in rule.allowed_objects:
        if normalized == allowed.lower():
            return allowed
    return ""


def _hangul_fragment_present(value: str) -> bool:
    return bool(re.search(r"[가-힣]", value or ""))


def generated_title_from_json(
    request: IntakeDetectionRequest,
    raw: str,
    *,
    rule: Optional[TitleGenerationRule] = None,
) -> str:
    """Validate a constrained JSON title draft and return a safe title or ''."""
    rule = rule or TitleGenerationRule()
    try:
        data = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    values = {key: data.get(key) for key in ("title", "action", "object")}
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        return ""

    title_value = str(values["title"])
    action_value = str(values["action"])
    object_value = str(values["object"])
    raw_title = " ".join(title_value.replace("\n", " ").split())
    stripped_title = _strip_chat_speaker_prefixes(raw_title)
    if raw_title != stripped_title:
        return ""
    if len(stripped_title) > rule.max_chars:
        return ""
    if _ID_LIKE_RE.search(stripped_title) or re.search(r"\d", stripped_title):
        return ""
    if _contains_sensitive_payload(stripped_title) or _hangul_fragment_present(stripped_title):
        return ""

    title = _compact_title(stripped_title, max_chars=rule.max_chars)
    words = title.split()
    if not (rule.min_words <= len(words) <= rule.max_words):
        return ""
    if _is_generic_title(title) or _is_clunky_title(title) or _looks_like_raw_user_title(title, request):
        return ""

    allowed_action = next((verb for verb in rule.allowed_verbs if action_value.strip().lower() == verb.lower()), "")
    if not allowed_action:
        return ""
    if not (title == allowed_action or title.lower().startswith(f"{allowed_action.lower()} ")):
        return ""

    safe_object = _safe_title_object(object_value, rule)
    if not safe_object:
        return ""
    if safe_object.lower() not in title.lower():
        return ""
    return title


def _should_attempt_title_generation(request: IntakeDetectionRequest, proposed_compact: str) -> bool:
    return (
        not proposed_compact
        or _is_generic_title(proposed_compact)
        or _is_clunky_title(proposed_compact)
        or _looks_like_raw_user_title(proposed_compact, request)
    )


def _enforce_non_raw_user_title(request: IntakeDetectionRequest, title: str) -> str:
    compact = _compact_title(title)
    if not compact or _looks_like_raw_user_title(compact, request):
        return _raw_title_fallback(request)
    return compact


def _clean_gate_text(request: IntakeDetectionRequest) -> str:
    return " ".join(f"{request.user_summary}\n{request.assistant_summary}".split())


def _has_explicit_card_request(text: str) -> bool:
    return bool(_EXPLICIT_CARD_REQUEST_RE.search(text))


def _has_proposal_record_request(text: str) -> bool:
    return bool(_PROPOSAL_RECORD_REQUEST_RE.search(text or ""))


def _has_direct_card_operation_request(text: str) -> bool:
    return bool(_DIRECT_CARD_OPERATION_RE.search(text or ""))


def _is_read_only_candidate_audit(text: str) -> bool:
    return bool(_CANDIDATE_AUDIT_RE.search(text) and _READ_ONLY_ACTION_RE.search(text))


def _has_explicit_user_card_request(request: IntakeDetectionRequest) -> bool:
    return _has_explicit_card_request(request.user_summary or "")


def _has_explicit_user_proposal_record_request(request: IntakeDetectionRequest) -> bool:
    return _has_proposal_record_request(request.user_summary or "")


def suppress_existing_card_update_intent(text: str) -> bool:
    """Return True for current-turn operations on an existing Kanban card.

    These messages may mention cards and progress, but they are commands to
    update/comment/record status on an existing tracking card, not requests to
    open a new card proposal banner.
    """
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return False
    if _STANDALONE_PROGRESS_UPDATE_RE.search(collapsed):
        return not (_has_durable_project_anchor(collapsed) and _has_concrete_followup_signal(collapsed))
    return bool(_EXISTING_CARD_REF_RE.search(collapsed) and _EXISTING_CARD_UPDATE_VERB_RE.search(collapsed))


def suppress_direct_card_operation_intent(request: IntakeDetectionRequest) -> bool:
    """Return True for current-turn direct Kanban card operations.

    Direct operations are handled by the main agent/tool path. Post-turn intake
    must not reinterpret them as default-board blocked proposal candidates. This
    check uses user text only so assistant wording cannot forge approval or
    durable follow-up intent.
    """
    user_text = " ".join(str(request.user_summary or "").split())
    if not user_text or _has_proposal_record_request(user_text):
        return False
    return _has_direct_card_operation_request(user_text)


def _is_approved_live_smoke_request(text: str) -> bool:
    return bool(_APPROVED_LIVE_SMOKE_RE.search(text))


def _is_vague_board_creation_discussion(text: str) -> bool:
    if not (_BOARD_REQUEST_RE.search(text) or "칸반보드" in text or "kanban board" in text.lower()):
        return False
    if _DURABLE_PROJECT_RE.search(text) and _CONCRETE_FOLLOWUP_RE.search(text):
        return False
    return True


def _is_meta_or_one_off(text: str) -> bool:
    return bool(_KANBAN_META_RE.search(text) or _ONE_OFF_QUESTION_RE.search(text))


def _has_durable_project_anchor(text: str) -> bool:
    return bool(_DURABLE_PROJECT_RE.search(text))


def _has_concrete_followup_signal(text: str) -> bool:
    return bool(_CONCRETE_FOLLOWUP_RE.search(text))


def card_proposal_eligibility(request: IntakeDetectionRequest, decision: Optional[DetectorDecision] = None) -> ProposalEligibility:
    text = _clean_gate_text(request)
    user_text = " ".join(str(request.user_summary or "").split())
    if _is_meta_or_one_off(text):
        return ProposalEligibility(False, "meta discussion or one-off question", "negative_meta_one_off", "ephemeral_workflow_command")
    if _is_approved_live_smoke_request(user_text):
        return ProposalEligibility(True, "approved live smoke request", "approved_live_smoke_request", "unsafe_live_side_effect")
    if suppress_existing_card_update_intent(user_text) and not _has_explicit_user_proposal_record_request(request):
        return ProposalEligibility(False, "existing card update intent", "existing_card_update_intent", "existing_card_update")
    if _is_read_only_candidate_audit(user_text) and not _has_explicit_user_proposal_record_request(request):
        return ProposalEligibility(False, "read-only candidate audit", "read_only_candidate_audit", "read_only_audit")
    if suppress_direct_card_operation_intent(request):
        return ProposalEligibility(False, "direct card operation intent", "direct_card_operation_intent", "direct_operation")
    if _has_explicit_user_proposal_record_request(request):
        return ProposalEligibility(True, "explicit card request", "explicit_card_request", "proposal_record_request")
    if _has_explicit_user_card_request(request):
        return ProposalEligibility(False, "direct card request requires main-turn handling", "direct_card_operation_intent", "direct_operation")
    if _is_vague_board_creation_discussion(text):
        return ProposalEligibility(False, "board discussion requires explicit long-lived project request", "board_requires_explicit_request", "insufficient_signal")
    if _has_durable_project_anchor(text) and _has_concrete_followup_signal(text):
        return ProposalEligibility(True, "durable project follow-up", "durable_followup", "durable_followup")
    return ProposalEligibility(False, "insufficient durable follow-up signal", "insufficient_scope", "insufficient_signal")


def _candidate_title_text(request: IntakeDetectionRequest) -> str:
    text = _CASUAL_TITLE_NOISE_RE.sub(" ", request.user_summary or "")
    # Prefer the segment that actually contains the card-worthy object instead
    # of carrying preceding conversational acknowledgements/restart chatter into
    # the card title.
    segments = [s.strip(" -:;,.，。") for s in re.split(r"(?:그리고|근데|,|，|\.|。|;|；)", text) if s.strip()]
    title_words = (
        "title generator",
        "타이틀",
        "제목",
        "kanban",
        "칸반",
        "gateway",
        "lifelog",
        "smoke",
        "복약",
        "medication",
        "수면",
        "sleep",
        "컨디션",
        "condition",
        "식단",
        "diet",
        "육아",
        "childcare",
        "운동",
        "training",
        "여행",
        "travel",
        "JÖKL",
        "jokl",
        "마케팅",
        "marketing",
        "packet",
        "패킷",
        "false positive",
        "false-positive",
        "오탐",
    )
    for segment in segments:
        if any(word.lower() in segment.lower() for word in title_words):
            return segment
    return text


def explicit_title_from_request(
    request: IntakeDetectionRequest,
    proposed_title: str = "",
    *,
    title_generator: Optional[Callable[[IntakeDetectionRequest, TitleGenerationRule], str]] = None,
) -> str:
    """Return a compact, action/object title for a Kanban intake card.

    Heuristic intake must not create vague cards like "Review ... follow-up and
    define next action". Titles should name the actual work item so the board is
    scannable without opening the card body. Caller-supplied titles are kept
    unless they are empty or generic boilerplate.
    """
    proposed_compact = _compact_title(proposed_title)
    should_generate = _should_attempt_title_generation(request, proposed_compact)
    if should_generate and title_generator is not None:
        rule = TitleGenerationRule()
        try:
            generated = generated_title_from_json(
                request,
                title_generator(_title_generator_request(request), rule),
                rule=rule,
            )
        except Exception:
            generated = ""
        if generated:
            return generated

    combined_for_title = "\n".join(
        part for part in (request.user_summary or "", proposed_compact, request.assistant_summary or "") if part
    )
    if (
        not proposed_compact
        or _is_generic_title(proposed_compact)
        or _is_clunky_title(proposed_compact)
        or _looks_like_raw_user_title(proposed_compact, request)
        or not evaluate_title_quality(proposed_compact, request).passed
    ):
        project_normalized = _normalize_project_title_intent(combined_for_title)
        if project_normalized:
            return project_normalized
        proposal_specific = _normalize_kanban_intake_title_intent(combined_for_title)
        if proposal_specific:
            return proposal_specific
        child_health = _normalize_child_family_health_title_intent(combined_for_title)
        if child_health:
            return child_health
        proposed_lifelog_normalized = _normalize_lifelog_title_intent(combined_for_title)
        if proposed_lifelog_normalized:
            return proposed_lifelog_normalized
        sensitive_ops = _normalize_sensitive_ops_title_intent(combined_for_title)
        if sensitive_ops:
            return sensitive_ops
    proposed_normalized = _normalize_korean_title_intent(
        "\n".join(part for part in (request.user_summary or "", proposed_compact) if part)
    )
    if proposed_normalized:
        return proposed_normalized
    if (
        proposed_compact
        and not _is_generic_title(proposed_compact)
        and not _is_clunky_title(proposed_compact)
        and evaluate_title_quality(proposed_compact, request).passed
    ):
        return _enforce_non_raw_user_title(request, proposed_compact)

    text = _candidate_title_text(request)
    lowered = text.lower()

    project_normalized = _normalize_project_title_intent(text)
    if project_normalized:
        return project_normalized
    kanban_normalized = _normalize_kanban_intake_title_intent(text)
    if kanban_normalized:
        return kanban_normalized
    child_health = _normalize_child_family_health_title_intent(text)
    if child_health:
        return child_health
    lifelog_normalized = _normalize_lifelog_title_intent(text)
    if lifelog_normalized:
        return lifelog_normalized
    sensitive_ops = _normalize_sensitive_ops_title_intent(text)
    if sensitive_ops:
        return sensitive_ops
    if "title generator" in lowered and ("kanban" in lowered or "칸반" in text):
        return "Improve Kanban intake title generator"
    if "live smoke" in lowered and ("lifelog-control" in lowered or "discord" in lowered):
        return "Verify Discord live smoke for lifelog-control"
    if "conversational intake" in lowered and ("kanban" in lowered or "칸반" in text):
        return "Improve conversational Kanban intake"
    if "gateway" in lowered and ("구현" in text or "implement" in lowered):
        return "Implement gateway follow-up work"
    if ("gateway" in lowered or "게이트웨이" in text) and ("restart" in lowered or "재시작" in text):
        return "Verify gateway restart follow-up"
    normalized = _normalize_korean_title_intent(text)
    if normalized:
        return normalized
    if "칸반" in text or "kanban" in lowered:
        return _enforce_non_raw_user_title(request, text or "Kanban follow-up")
    if "구현" in text:
        return _enforce_non_raw_user_title(request, f"Implement {text.replace('구현', '').strip() or 'follow-up work'}")
    if "검증" in text:
        return _enforce_non_raw_user_title(request, f"Verify {text.replace('검증', '').strip() or 'follow-up work'}")
    return _enforce_non_raw_user_title(request, text or "Follow up on conversation")


class KeywordHeuristicDetector:
    """Small deterministic detector for tests/local smoke; fails closed by default."""

    _CARD_WORDS = ("구현", "계획", "후속", "작업", "TODO", "카드", "card", "kanban", "lifelog", "gateway", "smoke")

    def detect(self, request: IntakeDetectionRequest) -> DetectorDecision:
        haystack = f"{request.user_summary}\n{request.assistant_summary}".lower()
        if not any(word.lower() in haystack for word in self._CARD_WORDS):
            return DetectorDecision(False)
        eligibility = card_proposal_eligibility(request)
        if not eligibility.eligible:
            return DetectorDecision(False, why=eligibility.reason, candidate_class=eligibility.candidate_class)
        title = explicit_title_from_request(request)
        return DetectorDecision(
            True,
            title=title,
            domain="lifelog-core",
            tenant=request.default_tenant or "lifelog",
            proposed_status="blocked",
            why=eligibility.reason,
            candidate_class=eligibility.candidate_class,
            body={
                "source_ref": request.source_ref,
                "acceptance_criteria": ["review proposed scope", "define next action"],
                "stop_conditions": ["live DB/cron/Graphify/public mutation needs separate approval"],
                "verification": "blocked review card exists on configured board",
            },
        )


class AuxiliaryLLMDetector:
    """Strict JSON adapter around a caller-supplied auxiliary function.

    The callable keeps this module independent from any specific provider path;
    gateway wiring can pass an auxiliary client later. If disabled or unsafe,
    detection returns no proposal.
    """

    def __init__(self, cfg: KanbanIntakeConfig, call_json: Optional[Callable[[IntakeDetectionRequest], str]] = None):
        self.cfg = cfg
        self.call_json = call_json

    def detect(self, request: IntakeDetectionRequest) -> DetectorDecision:
        if not self.cfg.auxiliary_detector_enabled or not self.cfg.redact_before_auxiliary or self.call_json is None:
            return DetectorDecision(False)
        try:
            return parse_detector_json(self.call_json(request))
        except Exception:
            return DetectorDecision(False)


def parse_detector_json(raw: str) -> DetectorDecision:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return DetectorDecision(False)
    if not isinstance(data, dict) or not data.get("card_worthy"):
        return DetectorDecision(False)
    status = str(data.get("status") or data.get("proposed_status") or "blocked").strip().lower()
    title = str(data.get("title") or "").strip()
    if not title or status not in _ALLOWED_STATUSES:
        return DetectorDecision(False)
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    decision = DetectorDecision(
        True,
        title=title,
        domain=str(data.get("domain") or "lifelog-core").strip() or "lifelog-core",
        tenant=str(data.get("tenant") or "lifelog").strip() or "lifelog",
        proposed_status=status,
        why=str(data.get("why") or "").strip(),
        body=body,
    )
    probe = KanbanCardProposal(
        board="dummy", title=decision.title, body=decision.body, source_ref="kp_probe",
        user_id="probe", proposed_status=decision.proposed_status, domain=decision.domain,
        tenant=decision.tenant, why=decision.why,
    )
    ok, _ = validate_proposal(probe, KanbanIntakeConfig(default_board="dummy"))
    return decision if ok else DetectorDecision(False)


def build_detection_request(
    *,
    source: Any,
    session_key: str,
    user_message: str,
    assistant_response: str,
    cfg: KanbanIntakeConfig,
) -> IntakeDetectionRequest:
    platform_obj = getattr(source, "platform", "")
    platform = getattr(platform_obj, "value", platform_obj)
    source_ref = "kp_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{platform}:{getattr(source, 'chat_id', '')}:{getattr(source, 'thread_id', '')}:{session_key}:{time.time_ns()}",
    ).hex[:16]
    return IntakeDetectionRequest(
        platform=str(platform or ""),
        session_key=session_key,
        source_ref=source_ref,
        user_summary=minimize_for_detector(user_message),
        assistant_summary=minimize_for_detector(assistant_response),
        default_board=cfg.default_board,
        default_tenant=cfg.default_tenant,
    )


def proposal_from_decision(
    decision: DetectorDecision,
    request: IntakeDetectionRequest,
    binding: SourceBinding,
    cfg: KanbanIntakeConfig,
    *,
    title_generator: Optional[Callable[[IntakeDetectionRequest, TitleGenerationRule], str]] = None,
) -> KanbanCardProposal:
    return KanbanCardProposal(
        board=request.default_board,
        title=explicit_title_from_request(request, decision.title, title_generator=title_generator),
        body=decision.body or {
            "source_ref": request.source_ref,
            "acceptance_criteria": ["review and define follow-up"],
            "stop_conditions": ["separate approval for live side effects"],
        },
        source_ref=request.source_ref,
        user_id=binding.user_id,
        proposed_status=decision.proposed_status or cfg.default_status,
        domain=decision.domain or "lifelog-core",
        tenant=decision.tenant or cfg.default_tenant,
        assignee=cfg.default_assignee,
        why=decision.why or "card-worthy follow-up detected",
    ).normalized(cfg)
