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
