import pytest

from gateway.kanban_intake import KanbanCardProposal, KanbanIntakeConfig, PendingKanbanStore, SourceBinding
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource
from hermes_cli import kanban_db as kb


def source(user="u1"):
    return SessionSource(platform=Platform.DISCORD, chat_id="c1", chat_type="thread", thread_id="t1", user_id=user)


@pytest.mark.asyncio
async def test_short_approval_intercept_executes_and_uses_to_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for name in ["HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT"]:
        monkeypatch.delenv(name, raising=False)
    kb.create_board("lifelog-control", name="Lifelog Control")
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    store = PendingKanbanStore(cfg.store_path)
    bind = SourceBinding.from_source(source(), "s1")
    store.put_pending(KanbanCardProposal(
        board="lifelog-control", title="Intercept task", body={"source_ref": "kp_safe"}, source_ref="kp_safe", user_id="u1"
    ), bind, cfg)
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: store
    calls = {"n": 0}
    async def fake_to_thread(func, *args, **kwargs):
        calls["n"] += 1
        return func(*args, **kwargs)
    monkeypatch.setattr("gateway.run.asyncio.to_thread", fake_to_thread)
    event = MessageEvent(text="ㅇㅇ", message_type=MessageType.TEXT, source=source())
    msg = await runner._maybe_handle_kanban_intake_reply(event, "s1")
    assert "생성/검증 완료" in msg
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_slash_approve_is_not_hijacked(tmp_path):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    event = MessageEvent(text="/approve", message_type=MessageType.TEXT, source=source())
    assert await runner._maybe_handle_kanban_intake_reply(event, "s1") is None


@pytest.mark.asyncio
async def test_cross_user_approval_no_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.create_board("lifelog-control", name="Lifelog Control")
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    store = PendingKanbanStore(cfg.store_path)
    store.put_pending(KanbanCardProposal(
        board="lifelog-control", title="Cross user", body={"source_ref": "kp_safe"}, source_ref="kp_safe", user_id="u1"
    ), SourceBinding.from_source(source("u1"), "s1"), cfg)
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: store
    event = MessageEvent(text="승인", message_type=MessageType.TEXT, source=source("u2"))
    assert await runner._maybe_handle_kanban_intake_reply(event, "s1") is None
