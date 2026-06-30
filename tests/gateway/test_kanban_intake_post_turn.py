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
