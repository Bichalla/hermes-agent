import pytest

from gateway.kanban_intake import DetectorDecision, KanbanIntakeConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource


class Detector:
    def __init__(self, decision):
        self.decision = decision
        self.requests = []
    def detect(self, request):
        self.requests.append(request)
        return self.decision


def source(user="u1"):
    return SessionSource(platform=Platform.DISCORD, chat_id="1521423652547989615", chat_type="thread", thread_id="1521317738210132069", user_id=user)


@pytest.mark.asyncio
async def test_post_turn_stores_pending_and_renders_message(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(True, title="Safe ops follow-up", body={"source_ref": "kp_safe"}))
    event = MessageEvent(text="gateway 구현 후속", message_type=MessageType.TEXT, source=source(), message_id="1521423652547989615")
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "done")
    assert msg is not None
    assert "카드 후보 감지" in msg
    assert "Implement gateway follow-up work" in msg
    assert runner._kanban_intake_detector.requests
    payload = runner._kanban_intake_detector.requests[0].user_summary
    assert "1521423652547989615" not in payload


@pytest.mark.asyncio
async def test_post_turn_rewrites_generic_detector_title(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Review gateway restart follow-up and define next action",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text="게이트웨이재시작했고 칸반보드 title generator를 더 명시적으로 다듬자 너무 허접하다 ㅋㅋ",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521423652547989615",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "구현하자")
    assert msg is not None
    assert "Improve Kanban intake title generator" in msg
    assert "Review gateway restart follow-up" not in msg


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "카드 생성 조건이 너무 후한거 아닌가?",
    "칸반보드가 필요한거는 장기로 이어지는 프로젝트에 한정해야되는 거 아니야??",
    "방금 다른 스레드에서 또 카드후보 타이틀 줬는데 변한게 없어 이거 왜이래??",
    "이거 해커톤 관련해서 칸반보드 만들고 카드 만드는게 낫지 않나??",
    "내 헤르메스 프로젝트 전체 좀 보고 보드 또는 카드 후보로 올릴 수 있는 대상 뭔지 확인하고 추천 목록 작성해서 알려줘봐. (실행은 금지)",
    "보드/카드 후보만 추천해줘. 실행하지 말고 목록만.",
])
async def test_post_turn_does_not_render_for_meta_or_one_off_even_if_detector_says_true(tmp_path, text):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(True, title="Noisy Kanban proposal", body={"source_ref": "kp_safe"}))
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source(), message_id="1521543610456084602")
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "답변했다.")
    assert msg is None


@pytest.mark.asyncio
async def test_post_turn_read_only_candidate_audit_ignores_assistant_card_wording(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(True, title="Raw candidate audit", body={"source_ref": "kp_safe"}))
    event = MessageEvent(
        text="내 헤르메스 프로젝트 전체 좀 보고 보드 또는 카드 후보로 올릴 수 있는 대상 뭔지 확인하고 추천 목록 작성해서 알려줘봐. (실행은 금지)",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084602",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(
        event,
        "s1",
        event.text,
        "이건 카드로 남겨도 좋은 후보입니다.",
    )
    assert msg is None


@pytest.mark.asyncio
async def test_post_turn_renders_semantic_title_for_explicit_candidate_audit_card_request(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="[상현] Hermes 프로젝트 전체 보드 또는 카드 후보 추천 목록 작성",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text="이 작업은 카드로 남겨줘: Hermes 프로젝트 전체 보드/카드 후보 추천 목록 정리",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084603",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "후보 목록을 정리했다.")
    assert msg is not None
    assert "Review Hermes Kanban board/card candidates" in msg
    assert "[상현]" not in msg
    assert "추천 목록 작성" not in msg


@pytest.mark.asyncio
async def test_post_turn_rewrites_raw_clunky_korean_detector_title(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    raw_title = "[상현] 게이트웨이재시작했고 칸반보드 title generator를 더 명시적으로 다듬자 너무 허접하다 ㅋㅋ"
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title=raw_title,
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text="게이트웨이재시작했고 칸반보드 title generator를 더 명시적으로 다듬자. 구현/테스트까지 하자",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084602",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "구현하자")
    assert msg is not None
    assert "Improve Kanban intake title generator" in msg
    assert "게이트웨이재시작" not in msg
    assert "허접" not in msg


@pytest.mark.asyncio
async def test_post_turn_noop_for_false_unsafe_empty_or_command(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(False))
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source())
    assert await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "done") is None
    runner._kanban_intake_detector = Detector(DetectorDecision(True, title="아이 fever", body={"source_ref": "kp_safe"}))
    assert await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "done") is None
    cmd = MessageEvent(text="/kanban list", message_type=MessageType.TEXT, source=source())
    assert await runner._maybe_build_kanban_intake_proposal_message(cmd, "s1", cmd.text, "done") is None
    assert await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "") is None
