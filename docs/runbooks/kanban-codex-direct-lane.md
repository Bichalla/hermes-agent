# Kanban → Codex Direct Lane Runbook

This runbook separates reviewed offline source evidence from every live migration,
Codex/provider, gateway, and pilot-card action. The lane is default-OFF and is
restricted to the `lifelog-control` pilot board.

## Architecture boundary

- Kanban owns approval, immutable execution instructions, claims, one-attempt
  policy, reconciliation, and human-review handoff.
- `ExecutionBackend` is the only Port. `CodexDirectExecutionBackend` is an
  infrastructure adapter and owns no task-status or database transition.
- The gateway is only the composition root. It constructs and passes a backend
  registry; it does not decide lifecycle outcomes.
- A managed attempt is never automatically completed and is never automatically
  retried or returned to ready.
- A proven-dead outcome becomes one sticky `managed_review_required` handoff.
- PID/start identity mismatch or uncertain cleanup becomes one sticky
  `managed_execution_safety_stopped` event. The active run and durable handle
  remain preserved; no signal or false terminal-cleanup claim is allowed.

## Evidence boundary

The offline integration uses the real host adapter with a temporary native fake
executable. It verifies argv, process identity, cleanup, lifecycle transitions,
canaries, and zero provider calls. It is **not** evidence that an installed real
Codex version accepts the flags, that the real Codex sandbox is effective, or
that OAuth/provider transport works. Those require a separately approved real
canary.

## Gate 0 — source-only verification

No live database, config, gateway, Codex, provider, OAuth, or network mutation:

```bash
cd ~/.hermes/hermes-agent
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_execution.py \
  tests/hermes_cli/test_kanban_codex_backend.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_config.py \
  tests/gateway/test_kanban_watchers_mixin.py \
  tests/integration/test_kanban_codex_direct_no_live.py -q
```

Required source invariants:

- `kanban.codex_direct.enabled` is the literal boolean `false` by default;
- missing, null, string, integer, malformed, or non-mapping values remain OFF;
- only literal boolean `true` and board `lifelog-control` can compose a
  launch-capable registry;
- ordinary Kanban connection performs zero managed-execution DDL;
- registry-absent legacy profile dispatch remains unchanged;
- the integration explicitly reports fake-executable evidence, not real Codex
  sandbox evidence;
- all fake process groups are dead and outside/production hashes are unchanged.

## Gate 1 — read-only board status

Reading the real pilot board is a live-system read and requires explicit approval
for the exact board/path. It performs no DDL:

```bash
hermes kanban codex status --board lifelog-control --json
```

Before migration, require:

- `db_exists` reflects the actual board database;
- `migrated=false`;
- no managed table or immutable trigger is created by status or ordinary gateway
  startup.

If the status output shows a partial or malformed schema, stop. Do not repair it
with ad-hoc SQL.

## Gate 2 — disposable real Codex diagnostic

This is not authorized by offline implementation approval. Obtain a fresh
current-turn approval naming:

- one real Codex/provider/OAuth invocation;
- the exact temporary root and timeout;
- protected outside canaries;
- zero production mutation;
- cleanup and evidence fields.

The diagnostic must verify the installed Codex version and actual startup report
with both required settings:

```text
sandbox_workspace_write.exclude_slash_tmp=true
sandbox_workspace_write.exclude_tmpdir_env_var=true
```

It must also verify:

- writable scope is only the disposable worktree;
- writes to `/tmp`, `$TMPDIR`, and outside canaries are denied;
- model-command network access is denied while provider/OAuth inference remains
  available only for that one invocation;
- normal exit and forced timeout leave zero descendants.

Failure returns `NEW_SPIKE_REQUIRED` or `PIVOT`. It never widens credentials,
disables the sandbox, or counts offline fake evidence as a substitute.

## Gate 3 — explicit pilot-board migration

Requires a fresh approval naming the exact canonical pilot DB and a consistent
SQLite backup. Keep the feature OFF.

```bash
hermes kanban codex migrate --board lifelog-control --json
hermes kanban codex status --board lifelog-control --json
```

Required readback:

- canonical table definition;
- immutable INSERT, UPDATE, and DELETE triggers;
- exact columns, types, nullability, primary key, foreign key/delete action, and
  unique spec digest;
- `migrated=true` only when the complete structure matches.

Repeat status. Ordinary connect/status must remain read-only. Never enable the
feature as part of migration.

## Gate 4 — gateway restart with feature OFF

Requires separate approval for the gateway target:

```bash
hermes config set kanban.codex_direct.enabled false
hermes gateway restart
hermes gateway status
```

Verify:

- zero new managed claim, prepare, or release;
- an already-active managed run is still recovered before legacy lifecycle;
- a matching live process is terminated and read back;
- identity mismatch is not signalled and safety-stops;
- transient DB/lock contention preserves startup recovery for a later tick.

## Gate 5 — approve one exact blocked card

Card creation/selection and approval are separate live actions. Name the exact
blocked task ID, project repository, worktree projection, and board in the
approval.

Read-only preview/status first, then the separately approved write:

```bash
hermes kanban codex approve TASK_ID --board lifelog-control --json
```

Approval must atomically persist canonical immutable bytes and digest. Exact
replay is idempotent; changed projection or conflicting bytes must fail. After
approval, changes to title, body, project, worktree, branch, assignee, or runtime
must prevent launch and require human review.

## Gate 6 — enable one-card pilot

Requires a fresh activation approval after Gates 1–5 pass:

```bash
hermes config set kanban.codex_direct.enabled true
hermes gateway restart
hermes gateway status
```

Observe exactly one attempt for the approved card:

1. claim;
2. isolated worktree;
3. durable PID/PGID/kernel-start handle persisted and read back;
4. exact release;
5. exit/timeout/recovery reconciliation;
6. sticky human-review handoff.

Required outcomes:

- attempt count `1`;
- automatic done count `0`;
- return-to-ready/retry count `0`;
- residual tracked processes `0` for proven-dead outcomes;
- mismatch/uncertainty has one safety-stop, preserved run/handle, and no signal;
- human explicitly reviews and completes/rejects the work.

Continued autonomous operation needs another explicit approval. Otherwise leave
the feature OFF after the one-card pilot.

## Kill switch and rollback

Feature kill switch first; this does not remove additive schema:

```bash
hermes config set kanban.codex_direct.enabled false
hermes gateway restart
hermes gateway status
```

Verify no new managed launch. OFF must not abandon an existing active attempt:
startup recovery still reconciles it or safety-stops uncertainty.

Do not drop the managed table/triggers without a separately approved restore
plan and consistent backup. Source rollback restores only the Task 0 captured
preimages/absent markers for the approved task paths; never reset or clean the
whole dirty repository.

## Incident rules

Stop the lane and leave it OFF when any of these occurs:

- actual Codex startup scope differs from the reviewed contract;
- executable or staged identity changes;
- PID/start identity cannot be proven;
- process-group inspection is uncertain;
- timeout cleanup leaves survivors;
- immutable schema/spec readback differs;
- a second attempt, automatic done, or ready-return is observed;
- provider credentials or raw Codex output appear in events/comments/logged
  evidence.

Preserve the database row, run metadata, handle identity, bounded logs, hashes,
and event IDs. Do not auto-retry, manually reuse a PID, or claim terminal cleanup
without `DEAD` evidence.
