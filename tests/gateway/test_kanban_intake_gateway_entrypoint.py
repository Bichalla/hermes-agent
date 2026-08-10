from __future__ import annotations

from datetime import datetime
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.kanban_intake import (
    APPROVAL,
    ApprovalResult,
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource
from tools.workflow_authority import (
    _mint_host_current_turn_user_authority,
    bind_active_workflow_turn,
    bind_current_turn_user_authority,
    clear_current_turn_user_authority,
    fingerprint_user_action,
)


@pytest.mark.asyncio
async def test_authorized_discord_entrypoint_binds_exact_capability_into_tool(
    tmp_path,
    monkeypatch,
):
    import gateway.kanban_intake as intake
    import tools.kanban_tools as tool

    fake_dotenv = types.ModuleType("dotenv")
    setattr(fake_dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="33333333333333333",
        chat_type="thread",
        thread_id="33333333333333333",
        parent_chat_id="22222222222222222",
        scope_id="44444444444444444",
        user_id="11111111111111111",
        message_id="66666666666666666",
    )
    event = MessageEvent(
        text="approve this exact proposal",
        message_type=MessageType.TEXT,
        source=source,
        message_id="66666666666666666",
        reply_to_message_id="55555555555555555",
        reply_to_author_id="88888888888888888",
        reply_to_is_own_message=True,
        reply_to_text="canonical proposal message",
    )
    session_key = "agent:main:discord:thread:33333333333333333:11111111111111111"
    intake_cfg = KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
    )
    store = PendingKanbanStore(intake_cfg.store_path)
    binding = SourceBinding.from_source(
        source,
        session_key,
        message_id=event.message_id,
    )
    assert source.user_id is not None
    pending = store.put_pending(
        KanbanCardProposal(
            board="lifelog-control",
            title="Implement Kanban intake approval guardrail",
            body={
                "source_ref": "entrypoint-test",
                "acceptance_criteria": ["exact entrypoint capability"],
            },
            source_ref="entrypoint-test",
            user_id=source.user_id,
            tenant="lifelog",
            assignee="honbul",
        ),
        binding,
        intake_cfg,
    )
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        ["55555555555555555"],
    )

    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda source: True
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda source: None
    runner._cache_session_source = lambda session_key, source: None
    runner._is_session_run_current = lambda session_key, generation: True
    runner._begin_session_run_generation = lambda session_key: 1
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._get_guild_id = lambda event: 44444444444444444
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._kanban_intake_config = lambda: intake_cfg
    runner._kanban_intake_store = lambda cfg=None: store
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=session_key,
        session_id="entrypoint-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="thread",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    monkeypatch.setattr(
        tool,
        "load_config",
        lambda: {
            "kanban": {
                "conversational_intake": {
                    "enabled": True,
                    "default_board": "lifelog-control",
                    "store_path": str(intake_cfg.store_path),
                }
            }
        },
    )
    monkeypatch.setattr(
        intake,
        "execute_pending_approval",
        lambda selected, _cfg: ApprovalResult(
            True,
            "synthetic exact entrypoint owner",
            task_id="t_synthetic",
            verified=True,
            action=APPROVAL,
        ),
    )

    captured = {}

    async def _run_agent(**kwargs):
        captured["message"] = kwargs["message"]
        authority = _mint_host_current_turn_user_authority(
            turn_id="entrypoint-turn",
            source_role="user",
            session_scope="entrypoint-session",
            platform_scope="discord",
            user_message_index=0,
            user_action_fingerprint=fingerprint_user_action(event.text),
            source_event_fingerprint=fingerprint_user_action("entrypoint-event"),
            allowed_action_classes=frozenset(),
            allowed_operations=frozenset(),
            operation_target_grants=frozenset(),
            target_fingerprints=frozenset(),
        )
        bind_current_turn_user_authority(authority)
        bind_active_workflow_turn(
            authority.turn_id,
            authority.platform_scope,
            authority.session_scope,
        )
        try:
            captured["tool_result"] = tool._handle_kanban_intake_decision(
                {"action": APPROVAL, "proposal_ref": pending.pending_id}
            )
        finally:
            clear_current_turn_user_authority()
        return {
            "failed": True,
            "final_response": captured["tool_result"],
            "error": "synthetic early stop after tool",
            "messages": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    object.__setattr__(runner, "_run_agent", _run_agent)
    await runner._handle_message_with_agent(event, source, session_key, 1)

    assert pending.pending_id in captured["message"]
    assert "t_synthetic" in captured["tool_result"]
