from __future__ import annotations

import types
from datetime import datetime, timezone
from unittest.mock import patch

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
from gateway.session_context import clear_session_vars
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


def _cfg(tmp_path):
    return KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
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


def test_v6_blocked_only_stale_rows_fail_closed_and_semantic_duplicate_replays(tmp_path):
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
            ("kanban-intake-policy/v5", first.pending_id),
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
    row = store.review_pending(include_all=True, now=103)["items"][0]
    assert row["status"] == "needs_revalidation"


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


def test_discord_event_keeps_direct_created_at_and_reply_provenance():
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
