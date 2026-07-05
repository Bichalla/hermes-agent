from types import SimpleNamespace

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
async def test_post_turn_rewrites_generic_lifelog_detector_title_by_record_object(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Review lifelog follow-up work",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text="방금 복약 기록 후속 작업 정리하고 카드로 남겨줘",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1522060000000000001",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "후속 작업이 필요하다.")
    assert msg is not None
    assert "Review medication intake Lifelog capture" in msg
    assert "Review lifelog follow-up work" not in msg


@pytest.mark.asyncio
async def test_post_turn_uses_safe_injected_title_generator(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Review lifelog follow-up work",
        body={"source_ref": "kp_safe"},
    ))
    runner._kanban_title_generator = lambda _request, _rule: '{"title":"Investigate missed medication reminder regression","action":"Investigate","object":"medication reminder"}'
    event = MessageEvent(
        text="lifelog medication reminder cron 누락 재발 방지 테스트 카드로 남겨줘",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1522060000000000003",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "후속 작업이 필요하다.")
    assert msg is not None
    assert "Investigate missed medication reminder regression" in msg
    assert "Review lifelog follow-up work" not in msg


@pytest.mark.asyncio
async def test_post_turn_uses_configured_constrained_llm_title_generator(tmp_path, monkeypatch):
    cfg = KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
        title_generator_enabled=True,
        title_generator_mode="constrained_llm",
    )
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"title":"Investigate missed medication reminder regression","action":"Investigate","object":"medication reminder"}'
            ))]
        )

    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, "call_llm", fake_call_llm)
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    configured = cfg
    runner._kanban_intake_store = lambda cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(configured.store_path)
    setattr(runner, "_kanban_intake_detector", Detector(DetectorDecision(
        True,
        title="Review lifelog follow-up work",
        body={"source_ref": "kp_safe"},
    )))
    event = MessageEvent(
        text="lifelog medication reminder cron 누락 재발 방지 테스트 카드로 남겨줘",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1522060000000000005",
    )

    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "후속 작업이 필요하다.")

    assert msg is not None
    assert "Investigate missed medication reminder regression" in msg
    assert calls and calls[0]["task"] == "title_generation"
    assert "allowed_objects" in calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_post_turn_falls_back_when_injected_title_generator_is_unsafe(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Review lifelog follow-up work",
        body={"source_ref": "kp_safe"},
    ))
    runner._kanban_title_generator = lambda _request, _rule: '{"title":"[상현] lifelog medication reminder cron 누락 원인 분석","action":"Review","object":"medication reminder"}'
    event = MessageEvent(
        text="lifelog medication reminder cron 누락 재발 방지 테스트 카드로 남겨줘",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1522060000000000004",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "후속 작업이 필요하다.")
    assert msg is not None
    assert "Fix Lifelog medication reminder cron regression" in msg
    assert "[상현]" not in msg
    assert "누락 원인 분석" not in msg


@pytest.mark.asyncio
async def test_post_turn_rewrites_generic_lifelog_title_generator_bug(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Review lifelog follow-up work",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text="Review lifelog follow-up work 같은 generic title 문제를 카드로 남겨줘",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1522060000000000002",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "후속 작업이 필요하다.")
    assert msg is not None
    assert "Fix Kanban candidate title generation for Lifelog records" in msg
    assert "Review lifelog follow-up work" not in msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected", "forbidden"),
    [
        (
            "카드로 남겨줘: add regression tests for Kanban intake false positives",
            "Add Kanban intake false-positive regression tests",
            "medication",
        ),
        (
            "카드로 남겨줘: JÖKL 마케팅 패킷 생성기 다음 스프린트 작업을 정리하고 테스트/커밋까지 해야 해",
            "Plan JÖKL marketing packet generator sprint work",
            "Plan Kanban follow-up work",
        ),
        (
            "해수 열 기록 후속 작업 카드로 남겨줘",
            "Review child health Lifelog capture",
            "해수",
        ),
        (
            "token rotation follow-up 카드로 남겨줘",
            "Review security rotation scope",
            "child health",
        ),
    ],
)
async def test_post_turn_renders_quality_hardened_semantic_titles(tmp_path, text, expected, forbidden):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Review lifelog follow-up work",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source(), message_id="1522060000000000010")
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "후속 작업이 필요하다.")
    assert msg is not None
    assert expected in msg
    assert forbidden not in msg


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "카드 생성 조건이 너무 후한거 아닌가?",
    "칸반보드가 필요한거는 장기로 이어지는 프로젝트에 한정해야되는 거 아니야??",
    "방금 다른 스레드에서 또 카드후보 타이틀 줬는데 변한게 없어 이거 왜이래??",
    "이거 해커톤 관련해서 칸반보드 만들고 카드 만드는게 낫지 않나??",
    "내 헤르메스 프로젝트 전체 좀 보고 보드 또는 카드 후보로 올릴 수 있는 대상 뭔지 확인하고 추천 목록 작성해서 알려줘봐. (실행은 금지)",
    "보드/카드 후보만 추천해줘. 실행하지 말고 목록만.",
    "흠 근데 title generator 가 LLM의 장점을 활용해서 이름을 잘 생성하게끔 업데이트 한거 아니었나??\n\n---\n카드 후보 감지: 이건 Kanban에 blocked review 카드로 남기는 게 좋다.\nboard: lifelog-control\ntitle: Review family health Lifelog capture\ndomain: lifelog-core\ntenant: lifelog\nstatus: blocked\nwhy: durable project follow-up\nsafety: dispatch 없음, ready/running 아님, live DB/cron/Graphify/JÖKL public mutation 없음\n\n승인하려면 “승인/ㅇㅇ/고고”, 취소하려면 “취소”.\n\n또 이런식으로 나오는데??",
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
async def test_post_turn_suppresses_current_turn_subagent_review_command_even_if_detector_says_true(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="[상현] 리뷰까지 서브에이전트 시켜서 진행해봐",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text="리뷰까지 서브에이전트 시켜서 진행해봐",
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084604",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(
        event,
        "s1",
        event.text,
        "리뷰 3개 서브에이전트로 병렬 발사함.",
    )
    assert msg is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "t_5b858cd6 카드 업데이트 승인",
    "진행상태 업데이트",
    "기존 카드에 진행상태 업데이트해",
    "이 카드에 Task 4 완료 기록 남겨",
    "tracking card t_5b858cd6에 코멘트 추가",
])
async def test_post_turn_suppresses_existing_card_update_even_if_detector_says_true(tmp_path, text):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="[상현] Update existing tracking card",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084605",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(
        event,
        "s1",
        event.text,
        "Hermes Agent repo에서 Kanban conversational intake 중복 노이즈를 고치는 프롬프트를 작성했다. 구현/테스트/커밋 후 검증하면 된다.",
    )
    assert msg is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "t_deadbeef 카드에 repo URL https://github.com/NousResearch/hermes-agent 기록해줘",
    "t_deadbeef 카드에 PR URL https://github.com/NousResearch/hermes-agent/pull/123 남겨",
    "t_deadbeef 카드에 artifact link /tmp/report.pdf 기록 남겨",
    "t_deadbeef 카드에 verification summary: focused tests 5 passed 기록",
    "t_deadbeef 카드에 handoff note: 다음 worker는 prompt_builder.py부터 보면 됨 남겨",
])
async def test_post_turn_suppresses_existing_card_metadata_update_even_if_detector_says_true(tmp_path, text):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Existing card metadata update",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084606",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(
        event,
        "s1",
        event.text,
        "Detector says this might be a card-worthy Kanban follow-up.",
    )
    assert msg is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "assistant_response"), [
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "실제로 필요한 카드는 이미 `suttanipata-ko` 보드에 만들었어: `t_e9f4c088`.",
    ),
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "카드 생성 실패했어. 권한 문제를 먼저 해결해야 해.",
    ),
])
async def test_post_turn_suppresses_direct_card_operation_even_if_detector_says_true(tmp_path, text, assistant_response):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Improve Kanban intake title generator",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084607",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, assistant_response)
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
async def test_post_turn_rewrites_verbatim_detector_title_before_rendering(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    raw_title = "카드로 남겨줘: 카드 후보 타이틀 생성시 원문 복사 금지 구현/테스트"
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title=raw_title,
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(
        text=raw_title,
        message_type=MessageType.TEXT,
        source=source(),
        message_id="1521543610456084606",
    )
    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, "구현하자")
    assert msg is not None
    assert "Fix Kanban title raw-copy guardrail scope" in msg
    assert raw_title not in msg
    assert "원문 복사 금지 구현/테스트" not in msg


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
