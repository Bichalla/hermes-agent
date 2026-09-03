"""Foreground-only Change Gate durable release issuance.

This module owns no new authority.  It loads the current Kanban task's
frozen Change Gate artifacts, projects existing review evidence, mints a
raw-free durable release from the already-bound foreground user authority,
and persists it only when the enabled runtime contract validates.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from hermes_cli.change_gate import (
    ChangeGateReason,
    GateDecision,
    ReleasePurpose,
    canonical_sha256,
    issue_durable_release_artifact,
)

_HOST_AUTHORITY_RE = re.compile(
    r"^AUTHORIZE_HERMES_CHANGE_GATE_(CLAIM|G4) ([a-f0-9]{64})$"
)
_HOST_AUTHORITY_PREFIXES = (
    "AUTHORIZE_HERMES_CHANGE_GATE_CLAIM",
    "AUTHORIZE_HERMES_CHANGE_GATE_G4",
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
    reused: bool = False

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
        result["reused"] = self.reused
        return result


@dataclass(frozen=True, slots=True)
class ChangeGateHostAdapterResult:
    ok: bool
    status: str
    reason: str
    task_id: str | None = None
    purpose: str | None = None
    release_id: str | None = None
    release_sha256: str | None = None
    handoff_sha256: str | None = None
    evidence_sha256: str | None = None
    issue_reason: str | None = None
    review_count: int = 0
    candidate_count: int = 0
    eligibility_diagnostic: dict[str, object] | None = None
    issued_at_epoch: int | None = None
    expires_at_epoch: int | None = None

    @property
    def terminal(self) -> bool:
        return self.status != "ineligible"

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": "change-gate-host-adapter-result/v1",
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "terminal": self.terminal,
            "candidate_count": self.candidate_count,
            "review_count": self.review_count,
        }
        if self.task_id is not None:
            result["task_id"] = self.task_id
        if self.purpose is not None:
            result["purpose"] = self.purpose
        if self.release_id is not None:
            result["release_id"] = self.release_id
        if self.release_sha256 is not None:
            result["release_sha256"] = self.release_sha256
        if self.handoff_sha256 is not None:
            result["handoff_sha256"] = self.handoff_sha256
        if self.evidence_sha256 is not None:
            result["evidence_sha256"] = self.evidence_sha256
        if self.issue_reason is not None:
            result["issue_reason"] = self.issue_reason
        if self.eligibility_diagnostic is not None:
            result["eligibility_diagnostic"] = self.eligibility_diagnostic
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


def is_change_gate_host_control_text(value: object) -> bool:
    """Identify the reserved foreground CLAIM/G4 control namespace.

    Exact statements and malformed variants that begin with a reserved prefix
    are host-control turns.  They must never fall through to a provider.
    """

    return type(value) is str and value.startswith(_HOST_AUTHORITY_PREFIXES)


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

        transition_anchor = kb.derive_change_gate_transition_anchor(
            conn,
            task_id,
            purpose=parsed_purpose,
            artifacts=artifacts,
            reviews=reviews,
        )
        if transition_anchor is None:
            return _deny(
                task_id,
                parsed_purpose.value,
                ChangeGateReason.RELEASE_TRANSITION_STALE,
            )

        release = issue_durable_release_artifact(
            purpose=parsed_purpose,
            handoff=artifacts.handoff,
            evidence=artifacts.evidence,
            transition_anchor=transition_anchor,
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

        try:
            stored = kb.persist_or_reuse_foreground_change_gate_release(
                conn,
                release,
                board=board,
                now_epoch=now_epoch,
            )
        except kb.ChangeGateReleasePersistenceDenied as exc:
            return _deny(task_id, parsed_purpose.value, exc.reason)
        return ChangeGateReleaseIssueResult(
            True,
            ChangeGateReason.ALLOWED.value,
            task_id,
            parsed_purpose.value,
            release_id=stored.release.release_id,
            release_sha256=canonical_sha256(stored.release),
            handoff_sha256=stored.release.handoff_sha256,
            evidence_sha256=stored.release.evidence_sha256,
            review_count=len(reviews),
            issued_at_epoch=stored.release.issued_at_epoch,
            expires_at_epoch=stored.release.expires_at_epoch,
            reused=stored.reused,
        )


def _host_ineligible(reason: str) -> ChangeGateHostAdapterResult:
    return ChangeGateHostAdapterResult(False, "ineligible", reason)


def _eligibility_with_issue(
    diagnostic: dict[str, object] | None,
    *,
    ok: bool,
    reason: str,
) -> dict[str, object]:
    receipt = dict(diagnostic or {})
    receipt.setdefault(
        "pre_release_eligible_candidate_count",
        receipt.get("eligible_candidate_count", 0),
    )
    receipt["release_candidate_count"] = 1 if ok else 0
    receipt["release_issue"] = {
        "verdict": "PASS" if ok else "FAIL",
        "actual": {"reason": reason},
        "expected": ChangeGateReason.ALLOWED.value,
    }
    return receipt


def issue_current_turn_change_gate_release() -> ChangeGateHostAdapterResult:
    """Issue a Change Gate release from the exact current foreground turn."""

    from gateway.session_context import (
        get_session_controller_role,
        get_trusted_current_user_text,
    )
    from tools.workflow_authority import (
        get_current_turn_user_authority,
        matches_active_workflow_turn,
        matches_current_workflow_session,
    )

    trusted_text = get_trusted_current_user_text()
    match = _HOST_AUTHORITY_RE.fullmatch(trusted_text or "")
    if match is None:
        if is_change_gate_host_control_text(trusted_text):
            return ChangeGateHostAdapterResult(
                False,
                "zero_candidate",
                "change_gate_authority_statement_not_exact",
            )
        return _host_ineligible("current_turn_authority_statement_missing")

    authority = get_current_turn_user_authority()
    controller_role = get_session_controller_role()
    active_turn_ok = bool(
        authority is not None
        and matches_active_workflow_turn(authority, user_message=trusted_text)
    )
    current_session_ok = bool(
        authority is not None and matches_current_workflow_session(authority)
    )
    if (
        authority is None
        or controller_role != "main_controller"
        or not active_turn_ok
        or not current_session_ok
    ):
        return ChangeGateHostAdapterResult(
            False,
            "ineligible",
            "current_turn_authority_invalid",
            eligibility_diagnostic={
                "schema": "change-gate-eligibility-diagnostic/v1",
                "global_predicates": {
                    "authority.present": {
                        "verdict": "PASS" if authority is not None else "FAIL",
                        "actual": authority is not None,
                        "expected": True,
                    },
                    "controller.main": {
                        "verdict": (
                            "PASS" if controller_role == "main_controller" else "FAIL"
                        ),
                        "actual": controller_role,
                        "expected": "main_controller",
                    },
                    "current_turn.active_exact_text": {
                        "verdict": "PASS" if active_turn_ok else "FAIL",
                        "actual": active_turn_ok,
                        "expected": True,
                    },
                    "current_session.matches": {
                        "verdict": "PASS" if current_session_ok else "FAIL",
                        "actual": current_session_ok,
                        "expected": True,
                    },
                },
                "candidate_count": 0,
                "eligible_candidate_count": 0,
                "release_candidate_count": 0,
                "candidates": [],
            },
        )

    from hermes_cli import kanban_db as kb
    from hermes_cli.change_gate_runtime import load_runtime_policy

    policy = load_runtime_policy()
    if policy.enabled is not True:
        return _host_ineligible(ChangeGateReason.DISABLED.value)
    if policy.valid is not True:
        return _host_ineligible(ChangeGateReason.RUNTIME_CONFIG_INVALID.value)

    board = kb.get_current_board()
    try:
        with kb.connect_closing(board=board) as conn:
            target = kb.resolve_current_turn_change_gate_target(
                conn,
                policy=policy,
                board=board,
            )
    except Exception:
        return ChangeGateHostAdapterResult(
            False,
            "owner_failure",
            "change_gate_owner_failure",
        )

    if target.status == "zero_candidate":
        return ChangeGateHostAdapterResult(
            False,
            "zero_candidate",
            "change_gate_target_not_found",
            candidate_count=target.candidate_count,
            eligibility_diagnostic=target.diagnostic,
        )
    if target.status == "ambiguous":
        return ChangeGateHostAdapterResult(
            False,
            "ambiguous",
            "change_gate_target_ambiguous",
            candidate_count=target.candidate_count,
            eligibility_diagnostic=target.diagnostic,
        )
    if target.status != "resolved" or target.task_id is None or target.purpose is None:
        return ChangeGateHostAdapterResult(
            False,
            "owner_failure",
            "change_gate_owner_failure",
            candidate_count=target.candidate_count,
            eligibility_diagnostic=target.diagnostic,
        )

    try:
        issued = issue_change_gate_release(
            task_id=target.task_id,
            purpose=target.purpose.value,
        )
    except Exception:
        return ChangeGateHostAdapterResult(
            False,
            "owner_failure",
            "change_gate_owner_failure",
            task_id=target.task_id,
            purpose=target.purpose.value,
            candidate_count=target.candidate_count,
            issue_reason="change_gate_release_issue_exception",
            eligibility_diagnostic=_eligibility_with_issue(
                target.diagnostic,
                ok=False,
                reason="change_gate_release_issue_exception",
            ),
        )
    if type(issued) is not ChangeGateReleaseIssueResult:
        return ChangeGateHostAdapterResult(
            False,
            "owner_failure",
            "change_gate_owner_failure",
            task_id=target.task_id,
            purpose=target.purpose.value,
            candidate_count=target.candidate_count,
            eligibility_diagnostic=_eligibility_with_issue(
                target.diagnostic,
                ok=False,
                reason="change_gate_release_issue_result_invalid",
            ),
        )
    if not issued.ok:
        return ChangeGateHostAdapterResult(
            False,
            "owner_failure",
            "change_gate_owner_failure",
            task_id=target.task_id,
            purpose=target.purpose.value,
            candidate_count=target.candidate_count,
            review_count=issued.review_count,
            issue_reason=issued.reason,
            eligibility_diagnostic=_eligibility_with_issue(
                target.diagnostic,
                ok=False,
                reason=issued.reason,
            ),
        )
    return ChangeGateHostAdapterResult(
        True,
        "existing_idempotent" if issued.reused else "issued",
        issued.reason,
        task_id=target.task_id,
        purpose=target.purpose.value,
        release_id=issued.release_id,
        release_sha256=issued.release_sha256,
        handoff_sha256=issued.handoff_sha256,
        evidence_sha256=issued.evidence_sha256,
        review_count=issued.review_count,
        candidate_count=target.candidate_count,
        eligibility_diagnostic=_eligibility_with_issue(
            target.diagnostic,
            ok=True,
            reason=issued.reason,
        ),
        issued_at_epoch=issued.issued_at_epoch,
        expires_at_epoch=issued.expires_at_epoch,
    )


__all__ = [
    "ChangeGateHostAdapterResult",
    "ChangeGateReleaseIssueResult",
    "is_change_gate_host_control_text",
    "issue_current_turn_change_gate_release",
    "issue_change_gate_release",
]
