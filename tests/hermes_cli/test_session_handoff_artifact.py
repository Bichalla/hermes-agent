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


def test_build_handoff_filters_active_task_preserved_meta_from_latest_and_tail():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {"role": "user", "content": "real user request: evaluate handoff quality"},
            {"role": "assistant", "content": "evaluation complete. validation ok."},
            {
                "role": "user",
                "content": "[Your active task list was preserved across context compression] stale internal task text",
            },
        ],
        artifact_path="/tmp/handoff.md",
    )

    assert "real user request: evaluate handoff quality" in artifact.markdown
    assert "Your active task list was preserved" not in artifact.markdown
    assert "stale internal task text" not in artifact.markdown
    assert artifact.json_payload["quality_card"]["filtered_meta_messages"] == 1


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


def test_build_handoff_extracts_structured_file_command_commit_config_inventory():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Updated `/Users/honbul/.hermes/hermes-agent/hermes_cli/session_handoff.py`. "
                    "Ran `scripts/run_tests.sh tests/hermes_cli/test_session_handoff_artifact.py -- -q`. "
                    "Config key `session_handoff.on_reset.preview.enabled=true` verified. "
                    "Commit `9aacee1c4 feat(gateway): add reset handoff safe preview` exists."
                ),
            },
        ],
        artifact_path="/tmp/handoff.md",
    )

    inventory = artifact.json_payload["evidence_inventory"]
    assert "/Users/honbul/.hermes/hermes-agent/hermes_cli/session_handoff.py" in inventory["files"]
    assert "/Users/honbul/.hermes/hermes-agent" in inventory["repos"]
    assert "scripts/run_tests.sh tests/hermes_cli/test_session_handoff_artifact.py -- -q" in inventory["commands"]
    assert "session_handoff.on_reset.preview.enabled" in inventory["config_keys"]
    assert any(item.startswith("9aacee1c4") for item in inventory["commits"])
    assert artifact.json_payload["quality_card"]["structured_file_count"] == 1
    assert artifact.json_payload["quality_card"]["structured_command_count"] == 1
    assert "Not enough deterministic file/command evidence" not in artifact.markdown
    assert "## Files / Repos / Commands Involved" in artifact.markdown


def test_build_handoff_inventory_filters_hyphenated_sensitive_hints():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Do not surface `/Users/honbul/.hermes/api-key.txt`, "
                    "`/Users/honbul/.hermes/access-key.txt`, or `secret.config.token`."
                ),
            }
        ],
        artifact_path="/tmp/handoff.md",
    )

    inventory = artifact.json_payload["evidence_inventory"]
    inventory_section = artifact.markdown.split("## Files / Repos / Commands Involved", 1)[1].split(
        "## Known Failure Modes", 1
    )[0]
    assert inventory["files"] == []
    assert inventory["config_keys"] == []
    assert "api-key" not in inventory_section
    assert "access-key" not in inventory_section


def test_build_handoff_renders_completed_actions_as_bullets():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Config updated. hermes config check passed. "
                    "Commit `9aacee1c4` pushed. Next: wait for user approval before restart."
                ),
            }
        ],
        artifact_path="/tmp/handoff.md",
    )

    completed = artifact.json_payload["completed_actions"]
    assert completed == ["Config updated.", "hermes config check passed.", "Commit `9aacee1c4` pushed."]
    section = artifact.markdown.split("## Last Completed Action", 1)[1].split("## Open Loops", 1)[0]
    assert section.count("\n- ") >= 3
    assert "Next: wait for user approval" not in section
    assert "9aacee1c4" in artifact.json_payload["last_completed_action"]


def test_build_handoff_splits_bulleted_completed_actions_from_next_steps():
    from hermes_cli.session_handoff import build_handoff_artifact

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {
                "role": "assistant",
                "content": "- Config updated\n- Tests passed\n- Next: wait for user approval before restart",
            }
        ],
        artifact_path="/tmp/handoff.md",
    )

    assert artifact.json_payload["completed_actions"] == ["Config updated", "Tests passed"]
    assert artifact.json_payload["open_loops"] == "Next: wait for user approval before restart"
    completed_section = artifact.markdown.split("## Last Completed Action", 1)[1].split("## Open Loops", 1)[0]
    open_loop_section = artifact.markdown.split("## Open Loops / Follow-up Context", 1)[1].split("## Next Useful Context", 1)[0]
    assert "Next: wait" not in completed_section
    assert "Config updated" not in open_loop_section
    assert "Tests passed" not in open_loop_section


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


def test_safe_preview_includes_structured_inventory_counts_without_values():
    from hermes_cli.session_handoff import build_handoff_artifact, build_handoff_preview

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "Updated `/Users/honbul/.hermes/hermes-agent/hermes_cli/session_handoff.py`. "
                    "Ran `scripts/run_tests.sh tests/hermes_cli/test_session_handoff_artifact.py -- -q`. "
                    "Commit `9aacee1c4` pushed."
                ),
            }
        ],
        artifact_path="/tmp/handoff.md",
    )

    preview = build_handoff_preview(artifact, max_items=4, max_chars=600)

    assert "structured files=1" in preview
    assert "commands=1" in preview
    assert "commits=1" in preview
    assert "/Users/honbul" not in preview
    assert "scripts/run_tests.sh" not in preview
    assert "9aacee1c4" not in preview


def test_safe_preview_suppresses_open_loop_when_only_raw_user_signal_exists():
    from hermes_cli.session_handoff import build_handoff_artifact, build_handoff_preview

    artifact = build_handoff_artifact(
        session_id="sess-old",
        messages=[{"role": "user", "content": "SECRET personal medical detail abc123"}],
        artifact_path="/tmp/handoff.md",
    )

    preview = build_handoff_preview(artifact, max_items=4, max_chars=600)

    assert artifact.json_payload["open_loops"] == "Not enough evidence in transcript."
    assert artifact.json_payload["active_task"] == "Not enough deterministic active task evidence in transcript."
    assert "Latest user signal:" not in artifact.markdown.split("## Open Loops / Follow-up Context", 1)[1].split("## Next Useful Context", 1)[0]
    assert "SECRET personal medical detail" not in preview
    assert "Latest user signal" not in preview
    assert "Open loop: no deterministic follow-up found" in preview


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
