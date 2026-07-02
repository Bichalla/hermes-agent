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
    assert data["read_only_candidate_audit_suppressed"] is True
    assert data["existing_card_update_suppressed"] is True
    assert data["durable_status_update_remains_eligible"] is True
    assert data["raw_source_ids_in_card_body"] is False
    assert data["sensitive_payload_in_card_body"] is False
