from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from agent.turn_context import build_turn_context
from gateway.kanban_intake import (
    APPROVAL,
    CURRENT_POLICY_VERSION,
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
    apply_typed_proposal_decision,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars
from hermes_cli import kanban_db as kb
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.e2e.conftest import (
    _make_discord_adapter_wired,
    make_discord_message,
    make_fake_dm_channel,
)
from tools.kanban_tools import KANBAN_INTAKE_DECISION_SCHEMA
from tools.workflow_authority import (
    _mint_host_current_turn_user_authority,
    bind_active_workflow_turn,
    bind_current_turn_user_authority,
    clear_current_turn_user_authority,
    extend_current_turn_user_authority_from_interactive_response,
    fingerprint_user_action,
    get_current_turn_user_authority,
    is_host_issued_current_turn_authority,
    matches_active_workflow_turn,
)


class _TodoStore:
    def has_items(self):
        return True


class _Guardrails:
    def reset_for_turn(self):
        pass


class _Agent:
    def __init__(self, platform: str = "cli"):
        self.session_id = "phase3-session"
        self.model = "test/model"
        self.provider = "test"
        self.base_url = ""
        self.api_key = ""
        self.api_mode = "chat_completions"
        self.platform = platform
        self._user_id = "sender"
        self.quiet_mode = True
        self.max_iterations = 1
        self.tools = []
        self.valid_tool_names = set()
        self.enabled_toolsets = None
        self.disabled_toolsets = None
        self._skip_mcp_refresh = False
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(
            protect_first_n=2, protect_last_n=2
        )
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _TodoStore()
        self._tool_guardrails = _Guardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self._pending_cli_user_message = None
        self._persist_calls = 0

    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _message):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_args):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _persist_session(self, *_args):
        self._persist_calls += 1


@pytest.fixture(autouse=True)
def _clean_context():
    clear_current_turn_user_authority()
    clear_session_vars([])
    yield
    clear_current_turn_user_authority()
    clear_session_vars([])


def _build(agent):
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        return build_turn_context(
            agent,
            "Create a blocked Kanban card.",
            None,
            None,
            "phase3-task",
            None,
            None,
            restore_or_build_system_prompt=lambda *a, **k: None,
            install_safe_stdio=lambda: None,
            sanitize_surrogates=lambda value: value,
            summarize_user_message_for_log=lambda value: value,
            set_session_context=lambda _value: None,
            set_current_write_origin=lambda _value: None,
            ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
        )


def test_production_foreground_turn_mints_host_issued_authority():
    _build(_Agent())
    authority = get_current_turn_user_authority()
    assert authority is not None
    assert is_host_issued_current_turn_authority(authority) is True
    assert matches_active_workflow_turn(authority) is True


def test_background_turn_receives_no_foreground_authority():
    _build(_Agent(platform="background"))
    assert get_current_turn_user_authority() is None


def test_interactive_extension_reseals_changed_authority_and_keeps_active_turn():
    current = _mint_host_current_turn_user_authority(
        turn_id="interactive-turn",
        source_role="user",
        session_scope="interactive-session",
        platform_scope="cli",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("please clarify"),
    )
    bind_current_turn_user_authority(current)
    bind_active_workflow_turn(
        current.turn_id, current.platform_scope, current.session_scope
    )

    updated = extend_current_turn_user_authority_from_interactive_response(
        "t_deadbeef 카드에 검증 결과 댓글 기록해줘"
    )

    assert updated is not None
    assert updated is not current
    assert updated.host_seal != current.host_seal
    assert is_host_issued_current_turn_authority(updated) is True
    assert matches_active_workflow_turn(updated) is True
    assert "검증 결과 댓글" not in repr(updated)


def test_interactive_reseal_rejects_wrong_active_turn_binding():
    current = _mint_host_current_turn_user_authority(
        turn_id="turn-d",
        source_role="user",
        session_scope="session-d",
        platform_scope="cli",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("clarify"),
    )
    bind_current_turn_user_authority(current)
    bind_active_workflow_turn("turn-d", "cli", "session-d")
    updated = extend_current_turn_user_authority_from_interactive_response(
        "t_deadbeef 카드에 결과 댓글 기록해줘"
    )

    assert updated is not None
    assert is_host_issued_current_turn_authority(updated) is True
    assert matches_active_workflow_turn(updated) is True

    bind_active_workflow_turn("different-turn", "cli", "different-session")
    assert matches_active_workflow_turn(updated) is False


def _cfg(tmp_path):
    return KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
        max_pending_per_session=4,
    )


def _binding(session="session-1"):
    return SourceBinding(
        platform="discord",
        chat_id="channel-1",
        thread_id="thread-1",
        user_id="user-1",
        session_key=session,
    )


def _proposal(*, source_ref="source-1", title="Implement v6 intake"):
    return KanbanCardProposal(
        board="lifelog-control",
        title=title,
        body={"source_ref": source_ref, "acceptance_criteria": ["blocked"]},
        source_ref=source_ref,
        user_id="user-1",
        assignee="honbul",
    )


@pytest.mark.parametrize(
    "stale_policy",
    ["kanban-intake-policy/v3", "kanban-intake-policy/v5"],
)
def test_v6_blocked_only_stale_rows_fail_closed_and_semantic_duplicate_replays(
    tmp_path, stale_policy
):
    assert CURRENT_POLICY_VERSION == "kanban-intake-policy/v6"
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    with pytest.raises(ValueError, match="status"):
        triage = _proposal()
        triage.proposed_status = "triage"
        store.put_pending(triage, _binding(), cfg, now=100)

    first = store.put_pending(_proposal(source_ref="source-a"), _binding(), cfg, now=100)
    replay = store.put_pending(_proposal(source_ref="source-b"), _binding(), cfg, now=101)
    assert replay.pending_id == first.pending_id
    assert replay.proposal.proposed_status == "blocked"

    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET policy_version=? WHERE pending_id=?",
            (stale_policy, first.pending_id),
        )
        conn.commit()
    result = apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=first.pending_id,
        binding=_binding(),
        cfg=cfg,
        store=store,
        now=102,
    )
    assert result.verified is False
    assert result.task_id is None
    stale_lookup = store.get_active_by_proposal_ref(
        first.pending_id, _binding(), now=103
    )
    assert stale_lookup.state == "invalid"
    assert stale_lookup.reason_code == "policy_version_mismatch"
    row = store.review_pending(include_all=True, now=103)["items"][0]
    assert row["status"] == "needs_revalidation"


def test_changed_effect_contract_is_distinct_from_semantic_replay(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()

    first = store.put_pending(
        _proposal(source_ref="source-a", title="Implement stable intake card"),
        binding,
        cfg,
        now=100,
    )
    replay = store.put_pending(
        _proposal(source_ref="source-b", title="Implement stable intake card"),
        binding,
        cfg,
        now=101,
    )
    changed = store.put_pending(
        _proposal(source_ref="source-c", title="Implement changed intake card contract"),
        binding,
        cfg,
        now=102,
    )

    assert replay.pending_id == first.pending_id
    assert changed.pending_id != first.pending_id
    assert changed.proposal_digest != first.proposal_digest
    assert store.get_active_by_proposal_ref(
        first.pending_id, binding, now=103
    ).state == "one"
    assert store.get_active_by_proposal_ref(
        changed.pending_id, binding, now=103
    ).state == "one"



def test_digest_delivery_replacement_and_typed_only_contract(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)
    old_id = "1521423652547989701"
    new_id = "1521423652547989702"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id, _binding(), [old_id], now=100.5
    )
    store.deny_delivery_authority(pending.pending_id)
    assert store.bind_outbound_proposal_messages(
        pending.pending_id, _binding(), [new_id], now=101
    )
    assert store.get_pending_capability_for_reply(
        _binding(), old_id, now=102
    ).state == "none"
    assert store.get_pending_capability_for_reply(
        _binding(), new_id, now=102
    ).state == "one"

    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET title=? WHERE pending_id=?",
            ("tampered", pending.pending_id),
        )
        conn.commit()
    assert store.get_active_by_proposal_ref(
        pending.pending_id, _binding(session="after-reset"), now=102
    ).reason_code == "proposal_digest_mismatch"

    params = KANBAN_INTAKE_DECISION_SCHEMA["parameters"]
    assert params["required"] == ["action", "proposal_ref"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"action", "proposal_ref"}


def test_typed_approval_creates_one_blocked_task_with_session_readback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    board = "lifelog-control"
    kb.create_board(board, name="Lifelog Control")
    cfg = KanbanIntakeConfig(
        enabled=True,
        default_board=board,
        store_path=tmp_path / "pending.db",
    )
    binding = _binding(session="composed-approval-session")
    proposal = _proposal(
        source_ref="approved-source",
        title="Implement composed blocked intake card",
    )
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(proposal, binding, cfg, now=100)
    message_id = "1521423652547990001"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id, binding, [message_id], now=100.5
    )

    canonical = store.get_active_by_proposal_ref(
        pending.pending_id, binding, now=101
    )
    assert canonical.state == "one"
    assert canonical.pending is not None
    result = apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=binding,
        cfg=cfg,
        store=store,
        now=102,
        expected_proposal_digest=canonical.pending.proposal_digest,
        expected_reply_message_id=message_id,
    )

    assert result.verified is True
    assert result.task_id is not None
    conn = kb.connect(board=board)
    try:
        tasks = kb.list_tasks(conn, include_archived=True)
    finally:
        conn.close()
    effect_tasks = [task for task in tasks if task.id == result.task_id]
    assert len(effect_tasks) == 1
    task = effect_tasks[0]
    assert task.status == "blocked"
    assert task.session_id == binding.session_key
    assert task.status not in {"ready", "running"}
    assert task.worker_pid is None
    assert task.claim_lock is None

    created_at = datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc)
    event = MessageEvent(
        text="latest batch",
        message_type=MessageType.TEXT,
        source=None,  # type: ignore[arg-type]
        message_id="1521423652547989703",
        timestamp=created_at,
        reply_to_message_id="1521423652547989704",
        reply_to_author_id="1521423652547989705",
        reply_to_is_own_message=True,
    )
    assert event.timestamp == created_at
    assert event.reply_to_message_id == "1521423652547989704"
    assert event.reply_to_is_own_message is True


@pytest.mark.asyncio
async def test_discord_adapter_direct_event_preserves_source_provenance():
    adapter, _runner = _make_discord_adapter_wired()
    assert isinstance(adapter, DiscordAdapter)
    adapter.handle_message = AsyncMock()
    adapter._text_batch_delay_seconds = 0
    client = adapter._client
    assert client is not None
    bot_user = client.user
    assert bot_user is not None
    created_at = datetime(2026, 8, 10, 12, 31, tzinfo=timezone.utc)
    message = make_discord_message(
        content="direct provenance",
        channel=make_fake_dm_channel(),
        message_id=1521423652547990002,
    )
    message.created_at = created_at
    resolved = types.SimpleNamespace(
        content="earlier bot message",
        author=bot_user,
    )
    message.reference = types.SimpleNamespace(
        message_id=1521423652547990003,
        resolved=resolved,
    )

    await adapter._handle_message(message)
    call = adapter.handle_message.await_args
    assert call is not None
    event = call.args[0]
    assert event.message_id == str(message.id)
    assert event.timestamp == message.created_at
    assert event.reply_to_message_id == "1521423652547990003"
    assert event.reply_to_author_id == str(bot_user.id)
    assert event.reply_to_is_own_message is True


@pytest.mark.asyncio
async def test_discord_batch_event_uses_latest_authenticated_provenance():
    adapter, _runner = _make_discord_adapter_wired()
    assert isinstance(adapter, DiscordAdapter)
    adapter.handle_message = AsyncMock()
    adapter._text_batch_delay_seconds = 60
    adapter._text_batch_split_delay_seconds = 60
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="batch-chat",
        chat_type="dm",
        user_id="batch-user",
    )
    first = MessageEvent(
        text="first chunk",
        source=source,
        message_id="1521423652547990004",
        timestamp=datetime(2026, 8, 10, 12, 32, tzinfo=timezone.utc),
        reply_to_message_id="1521423652547990005",
        reply_to_author_id="old-author",
        reply_to_is_own_message=False,
    )
    latest = MessageEvent(
        text="latest chunk",
        source=source,
        message_id="1521423652547990006",
        timestamp=datetime(2026, 8, 10, 12, 33, tzinfo=timezone.utc),
        reply_to_message_id="1521423652547990007",
        reply_to_author_id="latest-author",
        reply_to_is_own_message=True,
    )

    first_timestamp = first.timestamp
    first_reply_to_message_id = first.reply_to_message_id
    adapter._enqueue_text_event(first)
    adapter._enqueue_text_event(latest)
    key = adapter._text_batch_key(first)
    buffered = adapter._pending_text_batches[key]
    assert buffered.text == "first chunk\nlatest chunk"
    assert buffered.message_id == latest.message_id
    assert buffered.timestamp == latest.timestamp
    assert buffered.reply_to_message_id == latest.reply_to_message_id
    assert buffered.reply_to_author_id == latest.reply_to_author_id
    assert buffered.reply_to_is_own_message is True
    assert buffered.timestamp != first_timestamp
    assert buffered.reply_to_message_id != first_reply_to_message_id

    for task in list(adapter._pending_text_batch_tasks.values()):
        task.cancel()
    await asyncio.gather(
        *list(adapter._pending_text_batch_tasks.values()),
        return_exceptions=True,
    )
