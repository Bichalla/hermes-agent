# Kanban Conversational Intake

Gateway conversational intake is a default-off guardrail for proposing Kanban cards from ordinary chat context.

## Behavior

- The user does not need to know `/kanban create` syntax.
- When enabled, the gateway can detect card-worthy work after an agent turn and store one source-bound pending proposal.
- A short same-user reply such as `승인`, `ㅇㅇ`, or `고고` executes only that stored pending proposal.
- `취소`, `ㄴㄴ`, or `보류` denies the pending proposal without creating a card.
- Created cards are `blocked` by default, never `ready` or `running`.
- This path never dispatches a worker by itself; blocked intake cards require an explicit human promotion/unblock before worker execution.
- Generated titles use an action/object summary from the user request and replace generic boilerplate such as `Review ... follow-up and define next action`.

## Configuration

Default config is disabled:

```yaml
kanban:
  conversational_intake:
    enabled: false
    platforms: [discord]
    default_board: ""
    default_assignee: default
    default_tenant: lifelog
    default_status: blocked
    proposal_ttl_seconds: 1800
    max_pending_per_session: 1
    detector: heuristic
    auxiliary_detector_enabled: false
    redact_before_auxiliary: true
    pending_retention_seconds: 86400
    card_body_include_raw_source_ids: false
```

Live rollout requires separate approval to enable config and restart the gateway.

## Privacy and safety

- Pending store may contain raw platform/chat/thread/message/user IDs for operational binding only.
- Card bodies use opaque `source_ref` and safe summaries; raw source IDs are not copied into the Kanban card body.
- Auxiliary detector input must be minimized/redacted before any model call.
- Missing `user_id`, expired pending proposals, cross-user/thread replies, ambiguous multiple pending proposals, invalid status, missing board, or sensitive payload in card surfaces fail closed.
- Tests and smoke clear `HERMES_KANBAN_DB`, `HERMES_KANBAN_HOME`, `HERMES_KANBAN_BOARD`, and `HERMES_KANBAN_WORKSPACES_ROOT` before temp-home execution so they cannot hit a real board.

## No-live smoke

```bash
cd ~/.hermes/hermes-agent
venv/bin/python scripts/smoke_kanban_intake_no_live.py --json
```

Expected booleans include:

- `gateway_restarted: false`
- `discord_sent_live: false`
- `cron_mutated: false`
- `lifelog_db_mutated: false`
- `graphify_run: false`
- `kanban_env_overrides_cleared: true`
- `card_created_in_temp_home: true`
- `card_status: blocked`
- `card_blocked_by_default: true`
- `card_unclaimed_before_dispatch: true`
- `blocked_card_not_dispatched: true`
- `approved_short_phrase: true`
- `cross_user_fail_closed: true`
- `missing_user_id_fail_closed: true`
- `raw_source_ids_in_card_body: false`
- `sensitive_payload_in_card_body: false`
