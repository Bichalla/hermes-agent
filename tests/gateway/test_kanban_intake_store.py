from gateway.kanban_intake import (
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
)


def cfg(tmp_path):
    return KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")


def binding(user="u1", thread="t1", session="s1"):
    return SourceBinding(platform="discord", chat_id="c1", thread_id=thread, user_id=user, session_key=session)


def proposal(user="u1", title="follow up"):
    return KanbanCardProposal(
        board="lifelog-control",
        title=title,
        body={"source_ref": "kp_safe", "acceptance_criteria": ["check"]},
        source_ref="kp_safe",
        user_id=user,
    )


def test_put_and_lookup_same_source(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(), binding(), c, now=100)
    found = store.get_active_for_source(binding(), now=101)
    assert found.state == "one"
    assert found.pending.pending_id == pending.pending_id


def test_expired_cross_user_and_cross_thread_fail_closed(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    store.put_pending(proposal(), binding(), c, now=100)
    assert store.get_active_for_source(binding(), now=100 + c.proposal_ttl_seconds + 1).state == "none"
    assert store.get_active_for_source(binding(user="u2"), now=101).state == "none"
    assert store.get_active_for_source(binding(thread="other"), now=101).state == "none"


def test_multiple_active_is_ambiguous(tmp_path):
    c = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db", max_pending_per_session=2)
    store = PendingKanbanStore(c.store_path)
    store.put_pending(proposal(title="a"), binding(), c, now=100)
    store.put_pending(proposal(title="b"), binding(), c, now=101)
    found = store.get_active_for_source(binding(), now=102)
    assert found.state == "ambiguous"
    assert found.count == 2


def test_default_one_pending_limit_rejects_second(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    store.put_pending(proposal(title="a"), binding(), c, now=100)
    try:
        store.put_pending(proposal(title="b"), binding(), c, now=101)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("second active pending proposal should be rejected by default")


def test_missing_user_id_rejected(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    bad = SourceBinding(platform="discord", chat_id="c1", thread_id="t1", user_id="", session_key="s1")
    try:
        store.put_pending(proposal(user=""), bad, c)
    except ValueError as exc:
        assert "user_id" in str(exc)
    else:
        raise AssertionError("missing user_id should fail closed")


def test_purge_removes_old_non_pending(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(), binding(), c, now=100)
    store.mark_status(pending.pending_id, "denied", now=101)
    assert store.purge(now=100 + c.pending_retention_seconds + 1) == 1
