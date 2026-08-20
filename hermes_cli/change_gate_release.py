"""Foreground-only Change Gate durable release issuance.

This module owns no new authority.  It loads the current Kanban task's
frozen Change Gate artifacts, projects existing review evidence, mints a
raw-free durable release from the already-bound foreground user authority,
and persists it only when the enabled runtime contract validates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from hermes_cli.change_gate import (
    ChangeGateReason,
    GateDecision,
    ReleasePurpose,
    canonical_sha256,
    issue_durable_release_artifact,
)


@dataclass(frozen=True, slots=True)
class ChangeGateReleaseIssueResult:
    ok: bool
    reason: str
    task_id: str
    purpose: str
    release_id: str | None = None
    release_sha256: str | None = None
    handoff_sha256: str | None = None
    evidence_sha256: str | None = None
    review_count: int = 0
    issued_at_epoch: int | None = None
    expires_at_epoch: int | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": "change-gate-release-issue-result/v1",
            "ok": self.ok,
            "reason": self.reason,
            "task_id": self.task_id,
            "purpose": self.purpose,
            "review_count": self.review_count,
        }
        if self.release_id is not None:
            result["release_id"] = self.release_id
        if self.release_sha256 is not None:
            result["release_sha256"] = self.release_sha256
        if self.handoff_sha256 is not None:
            result["handoff_sha256"] = self.handoff_sha256
        if self.evidence_sha256 is not None:
            result["evidence_sha256"] = self.evidence_sha256
        if self.issued_at_epoch is not None:
            result["issued_at_epoch"] = self.issued_at_epoch
        if self.expires_at_epoch is not None:
            result["expires_at_epoch"] = self.expires_at_epoch
        return result


def _deny(task_id: str, purpose: str, reason: object) -> ChangeGateReleaseIssueResult:
    value = reason.value if isinstance(reason, ChangeGateReason) else str(reason)
    return ChangeGateReleaseIssueResult(False, value, task_id, purpose)


def _parse_purpose(value: object) -> ReleasePurpose | None:
    if value == ReleasePurpose.CLAIM.value:
        return ReleasePurpose.CLAIM
    if value == ReleasePurpose.G4.value:
        return ReleasePurpose.G4
    return None


def issue_change_gate_release(
    *,
    task_id: str,
    purpose: str,
) -> ChangeGateReleaseIssueResult:
    """Issue and store one durable release for an enabled Change Gate task.

    ``task_id`` and ``purpose`` are the entire caller-controlled surface.  The
    current user authority, trusted user text, artifacts, reviews, TTL, and
    storage destination are all derived inside this process.
    """

    if type(task_id) is not str or not task_id.strip() or "\0" in task_id:
        return _deny("", str(purpose), "schema_invalid")
    parsed_purpose = _parse_purpose(purpose)
    if parsed_purpose is None:
        return _deny(task_id, str(purpose), ChangeGateReason.RELEASE_PURPOSE_UNSUPPORTED)

    from hermes_cli import kanban_db as kb
    from hermes_cli.change_gate_runtime import (
        evaluate_loaded_runtime,
        load_runtime_policy,
        load_task_gate_artifacts,
        project_upstream_reviews,
    )

    policy = load_runtime_policy()
    if policy.enabled is not True:
        return _deny(task_id, parsed_purpose.value, ChangeGateReason.DISABLED)
    if policy.valid is not True:
        return _deny(task_id, parsed_purpose.value, ChangeGateReason.RUNTIME_CONFIG_INVALID)

    board = kb.get_current_board()
    now_epoch = int(time.time())
    with kb.connect_closing(board=board) as conn:
        kb.initialize_change_gate_runtime_schema(conn)
        load = load_task_gate_artifacts(
            conn,
            task_id,
            policy=policy,
            attachment_root=kb.task_attachments_dir(task_id, board=board),
        )
        if not load.ok or load.artifacts is None:
            return _deny(task_id, parsed_purpose.value, load.reason)

        artifacts = load.artifacts
        reviews = ()
        if parsed_purpose is ReleasePurpose.G4:
            projection = project_upstream_reviews(
                conn,
                task_id,
                handoff=artifacts.handoff,
            )
            if not projection.ok:
                return _deny(task_id, parsed_purpose.value, projection.reason)
            reviews = projection.reviews

        release = issue_durable_release_artifact(
            purpose=parsed_purpose,
            handoff=artifacts.handoff,
            evidence=artifacts.evidence,
            ttl_seconds=policy.release_ttl_seconds,
            clock=lambda: now_epoch,
        )
        if release is None:
            return _deny(task_id, parsed_purpose.value, ChangeGateReason.RELEASE_RECEIPT_MISSING)

        evaluation = evaluate_loaded_runtime(
            load,
            purpose=parsed_purpose,
            release=release,
            now_epoch=now_epoch,
            reviews=reviews,
        )
        if evaluation.result.decision is not GateDecision.ALLOW:
            return _deny(task_id, parsed_purpose.value, evaluation.result.reason)

        stored_release_id = kb.persist_foreground_change_gate_release(
            conn,
            release,
            board=board,
            now_epoch=now_epoch,
        )
        return ChangeGateReleaseIssueResult(
            True,
            ChangeGateReason.ALLOWED.value,
            task_id,
            parsed_purpose.value,
            release_id=stored_release_id,
            release_sha256=canonical_sha256(release),
            handoff_sha256=release.handoff_sha256,
            evidence_sha256=release.evidence_sha256,
            review_count=len(reviews),
            issued_at_epoch=release.issued_at_epoch,
            expires_at_epoch=release.expires_at_epoch,
        )


__all__ = [
    "ChangeGateReleaseIssueResult",
    "issue_change_gate_release",
]
