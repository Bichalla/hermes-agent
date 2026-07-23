# Registered Workflow Rollout Runbook

This runbook separates source verification from every live gate. Replace placeholders with exact reviewed paths. Never use globs.

## Gate 0 — source-only verification

No live DB, config, toolset, gateway, or network mutation:

```bash
cd ~/.hermes/hermes-agent
venv/bin/python scripts/smoke_registered_workflow_capabilities_no_live.py --json
```

Required result: `passed=true`, `mode=copied-source-default-deny`, `network_denied=true`, and `live_home_denied=true`.

Check current active profile remains absent in a separately isolated/read-only invocation:

```bash
venv/bin/python scripts/check_registered_workflow_active_readiness.py --expect-absent --json
```

## Gate 1 — explicit migrations (separate approval required)

Name every DB path in the approval. Back up each SQLite database consistently before apply.

Status-memory dry-run and two applies for each board DB:

```bash
venv/bin/python -m hermes_cli.kanban_capability_migrate \
  --db /ABSOLUTE/PATH/TO/BOARD.db --dry-run true --json
venv/bin/python -m hermes_cli.kanban_capability_migrate \
  --db /ABSOLUTE/PATH/TO/BOARD.db --dry-run false --json
venv/bin/python -m hermes_cli.kanban_capability_migrate \
  --db /ABSOLUTE/PATH/TO/BOARD.db --dry-run false --json
```

Pending-intake audit dry-run and two applies:

```bash
venv/bin/python -m hermes_cli.kanban_intake_migrate \
  --db /ABSOLUTE/PATH/TO/intake_pending.db --dry-run true --json
venv/bin/python -m hermes_cli.kanban_intake_migrate \
  --db /ABSOLUTE/PATH/TO/intake_pending.db --dry-run false --json
venv/bin/python -m hermes_cli.kanban_intake_migrate \
  --db /ABSOLUTE/PATH/TO/intake_pending.db --dry-run false --json
```

Required apply results: `ok=true`, `post_commit_reopen=true`, exact schema ready,
`integrity_check=ok`, row-count evidence present, and the immediately repeated apply
reports `changed=false`. Keep the JSON from every named board DB; readiness is not
complete if any board DB is omitted.

## Gate 2 — activate feature and Discord toolset (separate approval required)

```bash
hermes config set registered_workflow.enabled true
hermes tools enable --platform discord registered-workflow
hermes config check
```

Do not restart yet. Inspect the exact config/toolset diff and verify no unrelated toolset changed.

## Gate 3 — restart/new session (separate approval required)

```bash
hermes gateway restart
hermes gateway status
```

Start a new Discord session because tool schemas are cached per session. Then run:

```bash
cd ~/.hermes/hermes-agent
venv/bin/python scripts/check_registered_workflow_active_readiness.py --expect-ready --json
```

Required local result: `feature_enabled`, `tool_ready`, `pending_db_ready`, and
`board_dbs_ready` are true. The script enumerates the canonical default board and every
persisted named/archived board; callers cannot omit a board path. This script
does not inspect an already-running gateway process. In the newly created Discord
session, capture its actual tool list and verify `registered_local_workflow` is present;
the old session must not be used as evidence.

## Gate 4 — synthetic live canary (separate approval required)

Use one explicitly named low-risk target. Verify:

- prompt count is zero only for the registered typed action;
- exact idempotent replay does not duplicate a write;
- unrelated/ambiguous/foreign-target calls deny with zero writes;
- raw terminal invocation still follows Tirith and cannot invoke the review controller;
- no public message, deploy, credential, cron, hard delete, or migration occurs.

## Rollback

Feature kill switch first, then tool exposure rollback:

```bash
hermes config set registered_workflow.enabled false
hermes tools disable --platform discord registered-workflow
hermes gateway restart
hermes gateway status
```

Open a new session and verify:

```bash
cd ~/.hermes/hermes-agent
venv/bin/python scripts/check_registered_workflow_active_readiness.py --expect-absent --json
```

Capture the new session's actual tool list and verify `registered_local_workflow` is
absent. Invoke the registry handler directly with a fixed schema-valid synthetic request
and require `deny_feature_disabled`, `prompt_count=0`, and `write_count=0`; compare the
named pending/board DB main, WAL, and SHM digests before and after and require no change.
The local readiness script alone is not gateway-schema evidence.

Additive DB schema remains inert. Do not physically remove tables/triggers without a separately approved restore plan and consistent backup.
