from __future__ import annotations

import json
import stat
def test_build_handoff_includes_reference_only_header_and_intent_locks():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        new_session_id=None,
        platform="discord",
        source_label="test thread",
        messages=[{"role": "user", "content": "개인 프로젝트고 로컬 처리야"}],
        artifact_path="/tmp/handoff.md",
    )

    assert "[SESSION HANDOFF — REFERENCE ONLY]" in artifact.markdown
    assert "## Fresh Session Prompt" in artifact.markdown
    assert "## Intent Locks" in artifact.markdown
    assert "Sensitive data presence is not a reason" in artifact.markdown
    assert artifact.json_payload["session_id"] == "sess-old"


def test_build_handoff_truncates_evidence_tail_without_tool_results_by_default():
    from hermes_cli.session_handoff import build_handoff_artifact

    messages = [
        {"role": "user", "content": "u" * 5000},
        {"role": "tool", "content": "secret-ish tool payload"},
    ]

    artifact = build_handoff_artifact(
        session_id="sess-old",
        platform="discord",
        source_label="thread",
        messages=messages,
        artifact_path="/tmp/handoff.md",
        max_chars=1000,
        include_tool_results=False,
    )

    assert len(artifact.markdown) < 4000
    assert "secret-ish tool payload" not in artifact.markdown


def test_build_handoff_filters_compaction_noise_from_latest_and_tail():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {"role": "user", "content": "real request: record snack"},
            {"role": "assistant", "content": "record complete: event_id `diet_v1_abc`, validation ok"},
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] huge stale summary"},
        ],
        artifact_path="/tmp/handoff.md",
    )

    assert "real request: record snack" in artifact.markdown
    assert "[CONTEXT COMPACTION" not in artifact.markdown
    assert "huge stale summary" not in artifact.markdown


def test_build_handoff_separates_completed_action_from_open_loop():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {"role": "user", "content": "I ate snack A and milk B"},
            {
                "role": "assistant",
                "content": "record complete. event_id: `diet_v1_abc`. validation ok. Tonight: prioritize protein and low sodium.",
            },
        ],
        artifact_path="/tmp/handoff.md",
    )

    assert "## Last Completed Action" in artifact.markdown
    assert "record complete" in artifact.markdown
    assert "event_id: `diet_v1_abc`" in artifact.markdown
    assert "## Open Loops / Follow-up Context" in artifact.markdown
    assert "Tonight: prioritize protein" in artifact.markdown
    assert "## Active Task\n- Latest user request/evidence:" not in artifact.markdown


def test_build_handoff_includes_quality_card_counts_and_truncation_flags():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {"role": "user", "content": "real"},
            {"role": "tool", "content": "tool secret"},
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] noise"},
            {"role": "assistant", "content": "x" * 5000},
        ],
        artifact_path="/tmp/handoff.md",
        max_chars=2000,
        include_tool_results=False,
    )

    card = artifact.json_payload["quality_card"]
    assert card["raw_message_count"] == 4
    assert card["tool_messages_excluded"] == 1
    assert card["filtered_meta_messages"] == 1
    assert card["visible_message_count"] == 2
    assert card["truncated"] is True
    assert "## Handoff Quality" in artifact.markdown


def test_safe_preview_is_bounded_and_excludes_evidence_tail_and_tool_results():
    from hermes_cli.session_handoff import build_handoff_artifact, build_handoff_preview

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {"role": "user", "content": "private user detail should not be fully dumped"},
            {"role": "tool", "content": "secret tool result"},
            {
                "role": "assistant",
                "content": "기록 완료. event_id: `diet_v1_abc`. validation ok. Next: use low sodium dinner.",
            },
        ],
        artifact_path="/tmp/handoff.md",
        include_tool_results=False,
    )

    preview = build_handoff_preview(artifact, max_items=4, max_chars=300)

    assert preview.startswith("Preview:\n")
    assert "Last completed:" in preview
    assert "Open loop:" in preview
    assert "Evidence:" in preview
    assert "Inspect:" in preview
    assert "secret tool result" not in preview
    assert "private user detail" not in preview
    assert "diet_v1_abc" not in preview
    assert "low sodium dinner" not in preview
    assert "## Evidence Tail" not in preview
    assert len(preview) <= 300


def test_safe_preview_does_not_expose_raw_evidence_tail_marker():
    from hermes_cli.session_handoff import build_handoff_artifact, build_handoff_preview

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {"role": "user", "content": "show raw evidence?"},
            {"role": "assistant", "content": "record complete. validation ok."},
        ],
        artifact_path="/tmp/handoff.md",
    )

    preview = build_handoff_preview(artifact, max_items=4, max_chars=600)

    assert "## Evidence Tail" not in preview
    assert "user:" not in preview.lower()


def test_safe_preview_suppresses_open_loop_when_only_raw_user_signal_exists():
    from hermes_cli.session_handoff import build_handoff_artifact, build_handoff_preview

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[{"role": "user", "content": "SECRET personal medical detail abc123"}],
        artifact_path="/tmp/handoff.md",
    )

    preview = build_handoff_preview(artifact, max_items=4, max_chars=600)

    assert "SECRET personal medical detail" not in preview
    assert "Latest user signal" not in preview
    assert "Open loop:" in preview


def test_safe_preview_suppresses_assistant_echoed_sensitive_content():
    from hermes_cli.session_handoff import build_handoff_artifact, build_handoff_preview

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {
                "role": "assistant",
                "content": "record complete. Saved SECRET medical detail abc123. validation ok. Next: use SECRET medical detail abc123 for dinner.",
            }
        ],
        artifact_path="/tmp/handoff.md",
    )

    preview = build_handoff_preview(artifact, max_items=4, max_chars=600)

    assert "SECRET medical detail" not in preview
    assert "abc123" not in preview
    assert "completion evidence captured" in preview
    assert "follow-up context captured" in preview


def test_write_handoff_artifact_atomic_and_private(tmp_path):
    from hermes_cli.session_handoff import build_handoff_artifact, write_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess/old:unsafe",
        platform="discord",
        source_label="thread",
        messages=[{"role": "user", "content": "continue local work"}],
    )

    target_dir = tmp_path / "handoffs" / "default"
    written = write_handoff_artifact(
        artifact,
        artifact_dir=target_dir,
        session_id="sess/old:unsafe",
        timestamp="20260617T010203Z",
    )

    assert written.markdown_path.parent == target_dir
    assert written.json_path.parent == target_dir
    assert written.markdown_path.name == "20260617T010203Z-sess-old-unsafe.md"
    assert written.json_path.name == "20260617T010203Z-sess-old-unsafe.json"
    assert written.latest_path == target_dir / "latest.md"
    assert written.latest_path is not None
    assert written.markdown_path.read_text(encoding="utf-8").startswith("[SESSION HANDOFF")
    assert written.latest_path.read_text(encoding="utf-8").startswith("[SESSION HANDOFF")
    assert "continue local work" in written.latest_path.read_text(encoding="utf-8")
    assert json.loads(written.json_path.read_text(encoding="utf-8"))["session_id"] == "sess/old:unsafe"
    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(written.markdown_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.json_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.latest_path.stat().st_mode) == 0o600
    assert not list(target_dir.glob("*.tmp"))


def test_write_handoff_artifact_refuses_path_traversal(tmp_path):
    from hermes_cli.session_handoff import build_handoff_artifact, write_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="../../escape",
        messages=[{"role": "user", "content": "safe"}],
    )

    written = write_handoff_artifact(
        artifact,
        artifact_dir=tmp_path / "handoffs",
        session_id="../../escape",
        timestamp="20260617T010203Z",
    )

    assert written.markdown_path.parent == tmp_path / "handoffs"
    assert ".." not in written.markdown_path.name
    assert written.markdown_path.resolve().is_relative_to((tmp_path / "handoffs").resolve())
