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
_GENERIC_TITLE_RE = re.compile(
    r"(?:review\s+.*follow[-\s]?up|define\s+next\s+action|safe\s+ops\s+follow[-\s]?up|conversational\s+kanban\s+follow[-\s]?up)",
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
_KANBAN_CARD_ID_RE = r"\bt_[a-f0-9]{6,}\b"
_EXISTING_CARD_REF_RE = re.compile(
    rf"(?:{_KANBAN_CARD_ID_RE}|기존\s*카드|tracking\s*card|이\s*카드)",
    re.I,
)
_EXISTING_CARD_UPDATE_VERB_RE = re.compile(
    r"(?:업데이트|update|코멘트\s*추가|댓글\s*추가|comment|진행\s*상태|진행상태|status|progress|상태\s*기록|기록\s*남겨|남겨|완료\s*기록|승인)",
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


@dataclass(frozen=True)
class ProposalEligibility:
    eligible: bool
    reason: str
    matched_rule: str = ""


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
    return any(p.search(value) for p in _SENSITIVE_PATTERNS)


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
        return conn

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
                  why, idempotency_key, redaction_version, purge_after, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.pending_id, pending.created_at, pending.expires_at, pending.status,
                    binding.platform, binding.chat_id, binding.thread_id, binding.user_id, binding.session_key,
                    proposal.source_ref, json.dumps(pending.source_ids, ensure_ascii=False, sort_keys=True),
                    proposal.board, proposal.title, json.dumps(proposal.body, ensure_ascii=False, sort_keys=True),
                    proposal.domain, proposal.tenant, proposal.assignee, int(proposal.priority), proposal.proposed_status,
                    proposal.why, proposal.idempotency_key, "kanban-intake-redaction/v1", pending.purge_after, now,
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
    result = execute_pending_approval(lookup.pending, cfg)
    store.mark_status(lookup.pending.pending_id, "executed" if result.verified else "invalid")
    return result


def minimize_for_detector(text: str, *, max_chars: int = 500) -> str:
    value = str(text or "")
    value = _ID_LIKE_RE.sub("[id]", value)
    for pat in _SENSITIVE_PATTERNS:
        value = pat.sub("[sensitive]", value)
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


def _normalize_korean_title_intent(text: str) -> str:
    if _has_any(text, "타이틀", "title") and _has_any(text, "왜이래", "왜 이래", "변한게 없어", "카드후보"):
        return "Improve Kanban intake title normalization"
    if _has_any(text, "후보", "candidate") and _has_any(text, "보드", "카드", "kanban") and _has_any(text, "Hermes", "헤르메스"):
        return "Review Hermes Kanban board/card candidates"
    if _has_any(text, "해커톤", "hackathon") and _has_any(text, "칸반", "kanban") and _has_any(text, "카드", "board", "보드"):
        return "Create hackathon Kanban board backlog"
    if _has_any(text, "기존 카드", "기존 카드들") and _has_any(text, "기준", "지우", "어떻게"):
        return "Review existing lifelog-control Kanban cards"
    return ""


def _clean_gate_text(request: IntakeDetectionRequest) -> str:
    return " ".join(f"{request.user_summary}\n{request.assistant_summary}".split())


def _has_explicit_card_request(text: str) -> bool:
    return bool(_EXPLICIT_CARD_REQUEST_RE.search(text))


def _is_read_only_candidate_audit(text: str) -> bool:
    return bool(_CANDIDATE_AUDIT_RE.search(text) and _READ_ONLY_ACTION_RE.search(text))


def _has_explicit_user_card_request(request: IntakeDetectionRequest) -> bool:
    return _has_explicit_card_request(request.user_summary or "")


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
        return True
    return bool(_EXISTING_CARD_REF_RE.search(collapsed) and _EXISTING_CARD_UPDATE_VERB_RE.search(collapsed))


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
        return ProposalEligibility(False, "meta discussion or one-off question", "negative_meta_one_off")
    if _is_approved_live_smoke_request(user_text):
        return ProposalEligibility(True, "approved live smoke request", "approved_live_smoke_request")
    if suppress_existing_card_update_intent(user_text) and not _has_explicit_user_card_request(request):
        return ProposalEligibility(False, "existing card update intent", "existing_card_update_intent")
    if _is_read_only_candidate_audit(user_text) and not _has_explicit_user_card_request(request):
        return ProposalEligibility(False, "read-only candidate audit", "read_only_candidate_audit")
    if _has_explicit_user_card_request(request):
        return ProposalEligibility(True, "explicit card request", "explicit_card_request")
    if _is_vague_board_creation_discussion(text):
        return ProposalEligibility(False, "board discussion requires explicit long-lived project request", "board_requires_explicit_request")
    if _has_durable_project_anchor(text) and _has_concrete_followup_signal(text):
        return ProposalEligibility(True, "durable project follow-up", "durable_followup")
    return ProposalEligibility(False, "insufficient durable follow-up signal", "insufficient_scope")


def _candidate_title_text(request: IntakeDetectionRequest) -> str:
    text = _CASUAL_TITLE_NOISE_RE.sub(" ", request.user_summary or "")
    # Prefer the segment that actually contains the card-worthy object instead
    # of carrying preceding conversational acknowledgements/restart chatter into
    # the card title.
    segments = [s.strip(" -:;,.，。") for s in re.split(r"(?:그리고|근데|,|，|\.|。|;|；)", text) if s.strip()]
    title_words = ("title generator", "타이틀", "제목", "kanban", "칸반", "gateway", "lifelog", "smoke")
    for segment in segments:
        if any(word.lower() in segment.lower() for word in title_words):
            return segment
    return text


def explicit_title_from_request(request: IntakeDetectionRequest, proposed_title: str = "") -> str:
    """Return a compact, action/object title for a Kanban intake card.

    Heuristic intake must not create vague cards like "Review ... follow-up and
    define next action". Titles should name the actual work item so the board is
    scannable without opening the card body. Caller-supplied titles are kept
    unless they are empty or generic boilerplate.
    """
    proposed_compact = _compact_title(proposed_title)
    proposed_normalized = _normalize_korean_title_intent(
        "\n".join(part for part in (request.user_summary or "", proposed_compact) if part)
    )
    if proposed_normalized:
        return proposed_normalized
    if proposed_compact and not _is_generic_title(proposed_compact) and not _is_clunky_title(proposed_compact):
        return proposed_compact

    text = _candidate_title_text(request)
    lowered = text.lower()

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
        return _compact_title(text or "Kanban follow-up")
    if "구현" in text:
        return _compact_title(f"Implement {text.replace('구현', '').strip() or 'follow-up work'}")
    if "검증" in text:
        return _compact_title(f"Verify {text.replace('검증', '').strip() or 'follow-up work'}")
    return _compact_title(text or "Follow up on conversation")


class KeywordHeuristicDetector:
    """Small deterministic detector for tests/local smoke; fails closed by default."""

    _CARD_WORDS = ("구현", "계획", "후속", "작업", "TODO", "카드", "kanban", "lifelog", "gateway")

    def detect(self, request: IntakeDetectionRequest) -> DetectorDecision:
        haystack = f"{request.user_summary}\n{request.assistant_summary}".lower()
        if not any(word.lower() in haystack for word in self._CARD_WORDS):
            return DetectorDecision(False)
        eligibility = card_proposal_eligibility(request)
        if not eligibility.eligible:
            return DetectorDecision(False, why=eligibility.reason)
        title = explicit_title_from_request(request)
        return DetectorDecision(
            True,
            title=title,
            domain="lifelog-core",
            tenant=request.default_tenant or "lifelog",
            proposed_status="blocked",
            why=eligibility.reason,
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


def proposal_from_decision(decision: DetectorDecision, request: IntakeDetectionRequest, binding: SourceBinding, cfg: KanbanIntakeConfig) -> KanbanCardProposal:
    return KanbanCardProposal(
        board=request.default_board,
        title=explicit_title_from_request(request, decision.title),
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
