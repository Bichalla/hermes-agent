import pytest

from gateway.kanban_intake import (
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource


def source(user="u1"):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="c1",
        chat_type="thread",
        thread_id="t1",
        user_id=user,
    )


def proposal(user="u1"):
    return KanbanCardProposal(
        board="lifelog-control",
        title="Implement local guardrail scope",
        body={"source_ref": "kp_safe"},
        source_ref="kp_safe",
        user_id=user,
    )


@pytest.mark.asyncio
async def test_short_approval_is_not_preempted_before_main_llm(tmp_path, monkeypatch):
    cfg = KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
    )
    store = PendingKanbanStore(cfg.store_path)
    store.put_pending(proposal(), SourceBinding.from_source(source(), "s1"), cfg)
    runner = object.__new__(GatewayRunner)
    calls = {"n": 0}

    async def forbidden_to_thread(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("phrase intercept must not execute")

    monkeypatch.setattr("gateway.run.asyncio.to_thread", forbidden_to_thread)
    event = MessageEvent(text="ㅇㅇ", message_type=MessageType.TEXT, source=source())

    assert await runner._maybe_handle_kanban_intake_reply(event, "s1") is None
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_slash_approve_is_not_hijacked(tmp_path):
    runner = object.__new__(GatewayRunner)
    event = MessageEvent(text="/approve", message_type=MessageType.TEXT, source=source())
    assert await runner._maybe_handle_kanban_intake_reply(event, "s1") is None


@pytest.mark.asyncio
async def test_cross_user_phrase_cannot_mutate_pending(tmp_path):
    cfg = KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
    )
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(
        proposal("u1"),
        SourceBinding.from_source(source("u1"), "s1"),
        cfg,
    )
    runner = object.__new__(GatewayRunner)
    event = MessageEvent(text="승인", message_type=MessageType.TEXT, source=source("u2"))

    assert await runner._maybe_handle_kanban_intake_reply(event, "s1") is None
    found = store.get_active_for_source(
        SourceBinding.from_source(source("u1"), "s1")
    )
    assert found.pending is not None
    assert found.pending.pending_id == pending.pending_id
