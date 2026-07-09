import json
import subprocess
import sys


def test_no_live_smoke_script_outputs_safe_booleans(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    proc = subprocess.run(
        [sys.executable, "scripts/smoke_kanban_intake_no_live.py", "--json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["gateway_restarted"] is False
    assert data["discord_sent_live"] is False
    assert data["board_created_live"] is False
    assert data["cron_mutated"] is False
    assert data["lifelog_db_mutated"] is False
    assert data["graphify_run"] is False
    assert data["jokl_public_customer_mutation"] is False
    assert data["kanban_env_overrides_cleared"] is True
    assert data["card_created_in_temp_home"] is True
    assert data["card_status"] == "blocked"
    assert data["card_blocked_by_default"] is True
    assert data["card_unclaimed_before_dispatch"] is True
    assert data["blocked_card_not_dispatched"] is True
    assert data["approved_short_phrase"] is True
    assert data["cross_user_fail_closed"] is True
    assert data["missing_user_id_fail_closed"] is True
    assert data["one_off_card_proposal_suppressed"] is True
    assert data["meta_kanban_card_proposal_suppressed"] is True
    assert data["pasted_bad_proposal_meta_complaint_suppressed"] is True
    assert data["read_only_candidate_audit_suppressed"] is True
    assert data["existing_card_update_suppressed"] is True
    assert data["direct_card_operation_suppressed"] is True
    assert data["direct_card_operation_failure_suppressed"] is True
    assert data["completion_summary_existing_card_update_suppressed"] is True
    assert data["completion_summary_child_health_title_not_rendered"] is True
    assert data["old_policy_pending_approval_rejected"] is True
    assert data["durable_status_update_remains_eligible"] is True
    assert data["lifelog_generic_title_rewritten"] is True
    assert data["hybrid_title_generator_accepts_safe_draft"] is True
    assert data["hybrid_title_generator_rejects_unsafe_draft"] is True
    assert data["hybrid_title_generator_improves_semantic_bucket"] is True
    assert data["semantic_mismatch_title_generator_called"] is True
    assert data["semantic_mismatch_title_generator_corrects_sleep_title"] is True
    assert data["sleep_noon_reminder_not_medication"] is True
    assert data["live_adapter_uses_auxiliary_title_generation"] is True
    assert data["expired_pending_hygiene_flagged"] is True
    assert data["quality_metrics_present"] is True
    assert data["candidate_precision_threshold_met"] is True
    assert data["candidate_recall_threshold_met"] is True
    assert data["generic_title_rate_threshold_met"] is True
    assert data["raw_copy_rate_zero"] is True
    assert data["sensitive_title_leak_zero"] is True
    assert data["quality_thresholds_passed"] is True
    assert data["raw_source_ids_in_card_body"] is False
    assert data["sensitive_payload_in_card_body"] is False
