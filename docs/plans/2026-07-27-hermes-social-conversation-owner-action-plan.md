---
schema: pkm-frontmatter/v1
document_id: hermes-social-conversation-owner-action-plan-20260727
title: Hermes typed social_conversation_record owner action plan
created: 2026-07-27T11:55:00+09:00
updated: 2026-07-27T14:50:20+09:00
status: completed
risk_class: S3
execution_allowed: false
required_reviews: [security-privacy-spec-review]
public_surface_policy: fork-feature-branches-only
forbidden_without_confirmation: [gateway_restart, live_lifelog_write, active_dirty_checkout_merge, backup_delete]
---

# Hermes typed `social_conversation_record` owner action

**Goal:** Add one typed, service-gated owner route that records a confirmed self social conversation through the Lifelog `social_conversation.v1` recorder with a single host-trusted current Discord source, in-memory payload consumption, commit-time authority revalidation, and independent exact readback.

**Architecture:** Extend the existing `registered_local_workflow`; no new model tool. Two isolated task slices are required:

1. **ops contract:** expose an in-process `run_registered_social_document(document_bytes, pre_live_check, root)` path. It parses/normalizes the bytes once, derives all rows independently, performs dry-run, invokes the guard immediately before the transaction, writes from the held in-memory document, validates/readbacks/replays, and never reopens a payload pathname.
2. **Hermes core:** add host `scope_id` session context, typed action/capability/authority parser, exact one-source payload binding from one held no-follow fd, in-process ops adapter, pre-live guard, independent identity derivation, exact canonical DB readback, constant-safe tool result, and unconditional runtime-payload cleanup.

**Isolation identities:**
- Core repo `/Users/honbul/.hermes/hermes-agent`, isolated worktree `/Users/honbul/.hermes/worktrees/hermes-social-owner`, branch `feat/lifelog-social-conversation-owner-action`, base `b1186bd74`.
- Ops repo `/Users/honbul/.hermes/ops`, isolated worktree `/Users/honbul/.hermes/worktrees/hermes-ops-social-owner`, branch `feat/lifelog-social-owner-precommit-contract`, base `906ca53`.
- Active dirty checkouts are read-only during implementation; no merge/cherry-pick/restart in this task.

**Feedback Gate:** READY_FOR_REVIEW

## Grill Summary

1. **Model input:** only `action=social_conversation_record` plus an existing basename `payload_name`; no source, scope, path, DB, command, digest, target, authority, approval, or lifecycle option.
2. **Authority:** fixed target `person_park_sanghyun:social-conversation`, class `trusted_local_record`, exact operation, foreground current turn only.
3. **Host source:** extend `SessionSource.scope_id` through `gateway.run._set_session_env` into private task-local `HERMES_SESSION_SCOPE_ID`. The trusted tuple is `(platform, scope_id, chat_id, thread_id-or-chat_id, message_id)`.
4. **Payload provenance:** normalized `sources` must have exactly one entry and equal that trusted tuple byte-for-byte. Historical Discord message IDs are not persisted as provenance in this slice. The current approved record command is the sole source.
5. **Payload bytes:** core opens the original via `os.open` with no-follow, checks fd metadata (regular, current uid, link count 1, exact `0600`, bounded size), reads once, rechecks identical fd metadata, closes it, and never trusts the pathname content again. Ops consumes those exact in-memory bytes through a direct function call.
6. **Commit guard:** immediately before `BEGIN IMMEDIATE`, ops invokes a callback. The callback proves the same authority object is still current, `matches_active_workflow_turn`, session/platform/scope/chat/thread/message are unchanged, operation-target grant remains exact, and the in-memory digest/derived event/source expectations equal the original binding. No pathname is reopened.
7. **Independent identity:** both ops dispatcher and core independently normalize the closed JSON and derive canonical payload SHA-256, event ID, summary, source-ref, source-row ID, tags, and expected rows. Child/recorder output is evidence only and cannot select identity.
8. **Privacy lifecycle:** core securely reads every candidate through the verified fd to classify it. If closed-schema or current-source binding validation fails, the original file is retained unchanged for operator diagnosis and is never passed to the owner. Once a payload is schema-valid and current-source-bound, core unlinks that original in `finally` after every owner outcome (success, denial after binding, exception, or timeout). There is no invocation snapshot file. Social backups are private `0700/0600`, retained indefinitely, and can be deleted only by a separate explicitly approved backup-cleanup action; this slice never deletes backups.
9. **Output:** no raw payload, partner label, points, summary, source identifiers, scope, paths, argv, exception, or backup path.
10. **Gateway/live:** no restart and no canonical live record in this task.

## Task files

### Ops isolated branch

Copy only current social task files from the active ops checkout into the clean ops worktree, then modify only:
- `state/lifelog/scripts/record_social_conversation.py`
- `state/lifelog/scripts/run_registered_recorder.py`
- `state/lifelog/config/recorder-registry.json`
- `state/lifelog/tests/test_record_social_conversation.py`
- `state/lifelog/tests/test_run_registered_recorder.py`
- `state/lifelog/README.md`
- `docs/plans/2026-07-27-lifelog-social-conversation-recorder.md`

### Core isolated branch

- `gateway/session_context.py`
- `gateway/run.py`
- `tests/gateway/test_session_scope_context.py` (new, focused host scope propagation)
- `agent/workflow_action_policy.py`
- `tools/workflow_authority.py`
- `tools/registered_local_workflow.py`
- `tests/tools/test_registered_workflow_capability_policy.py`
- `tests/tools/test_workflow_authority.py`
- `tests/tools/test_registered_local_workflow.py`
- this plan

## Exact persisted-row contract

From normalized bytes, independently derive:

### `life_events` exact row

Columns and expectations:
- `id = social_v1_<sha256(identity-json)[:16]>`
- `occurred_at`, optional `ended_at`, `timezone` exactly normalized payload
- `event_type='note'`
- `title='중요한 대화 기록'`
- `summary='대화 상대: <label>. 확인된 본인 발언·결정·소회: ' + ' / '.join(points)`
- `source_type='discord'`
- `source_ref='discord:<channel_id>:<thread_id>:<message_id>'`
- `confidence` exact numeric `1.0`, not bool
- `raw_text_hash IS NULL`
- `payload_hash` independently derived 64-hex
- `created_at == updated_at`, valid UTC RFC3339 seconds. If inserted, timestamp is between owner invocation start/end. If existing, it is not in the future and all other row values still match.

### Related rows

- `life_event_people`: exactly one `(event_id, person_park_sanghyun, subject)`
- `life_event_tags`: exact normalized tag set/count
- `life_sources`: exactly one row:
  - deterministic `id = lifelog-<sha256('source|'+event_id+'|'+canonical_source_json)[:24]>`
  - exact event/platform/scope-as-guild/channel/thread/message
  - `path IS NULL`, `redacted_excerpt IS NULL`
  - `captured_at == life_events.created_at == life_events.updated_at`
- `life_metrics`: zero rows
- every other discovered table containing `event_id`: zero rows; discovered set must equal the pinned canonical extension inventory in tests
- `PRAGMA foreign_key_check`: empty

A coherent-alternate child result and DB row that agree with each other but differ from any independent expected field must be rejected.

## Closed tool result

Allow exact keys: common `_result` envelope plus `idempotency_result`, `validation_status`, `event_ids`, `payload_hash`, `backup_status`, `replay_status`, `payload_cleanup`, `readback`.

Exact states:
- decision allow; prompt count 0; write count exact int 0/1
- one valid social event ID and independent 64-hex payload hash
- validation `validator_and_exact_readback_passed`
- idempotency inserted/existing consistent with write count
- backup/replay `verified`, payload cleanup `deleted`, readback `passed`

All denial/error paths return only the common fixed envelope. Internal exceptions are normalized.

## Ordered TDD tasks and named seams

### Task O1 — Create ops worktree and RED in-memory seam

```bash
git -C /Users/honbul/.hermes/ops worktree add -b feat/lifelog-social-owner-precommit-contract /Users/honbul/.hermes/worktrees/hermes-ops-social-owner 906ca53
PY=/Users/honbul/.hermes/hermes-agent/venv/bin/python
cd /Users/honbul/.hermes/worktrees/hermes-ops-social-owner
unset PYTEST_ADDOPTS
$PY -m pytest state/lifelog/tests/test_record_social_conversation.py state/lifelog/tests/test_run_registered_recorder.py -q -o addopts=
```

Named RED tests:
- `test_social_document_live_consumes_held_bytes_not_replaced_path`
- `test_social_pre_live_guard_failure_calls_no_apply_live`
- `test_social_expected_identity_rejects_coherent_alternate_child`
- `test_social_exact_readback_rejects_each_event_field_tamper`
- `test_social_exact_readback_rejects_source_id_timestamp_and_nullability_tamper`
- `test_social_existing_timestamp_contract_and_replay`

### Task O2 — Implement ops in-memory contract and GREEN

Run the same literal command; require all tests pass and no canonical DB path appears in test output.

### Task C1 — Core session scope and authority RED

```bash
PY=/Users/honbul/.hermes/hermes-agent/venv/bin/python
cd /Users/honbul/.hermes/worktrees/hermes-social-owner
unset PYTEST_ADDOPTS
$PY -m pytest tests/gateway/test_session_scope_context.py tests/tools/test_workflow_authority.py tests/tools/test_registered_workflow_capability_policy.py -q -o addopts=
```

Named RED tests:
- `test_session_scope_id_is_task_local_bound_and_cleared`
- `test_gateway_binds_source_scope_id_to_session_context`
- `test_social_record_command_mints_exact_fixed_grant`
- `test_social_record_negation_plan_question_report_example_and_other_subject_mint_none`
- `test_social_capability_is_foreground_create_only`

### Task C2 — Core owner RED

```bash
$PY -m pytest tests/tools/test_registered_local_workflow.py -q -o addopts=
```

Named RED tests:
- `test_social_schema_adds_action_without_new_parameter`
- `test_social_payload_requires_exact_single_current_source_tuple`
- `test_social_payload_fd_mode_owner_link_size_and_closed_schema`
- `test_social_owner_revalidates_authority_at_pre_live_seam`
- `test_social_owner_reads_once_and_ignores_path_replacement`
- `test_social_owner_rejects_coherent_alternate_result_and_db`
- `test_social_owner_exact_result_and_constant_safe_output`
- `test_social_owner_unlinks_valid_bound_payload_on_success_failure_timeout`
- `test_social_denials_reach_no_ops_live_write`

### Task C3 — Implement core and GREEN/regression

```bash
PY=/Users/honbul/.hermes/hermes-agent/venv/bin/python
cd /Users/honbul/.hermes/worktrees/hermes-social-owner
unset PYTEST_ADDOPTS
$PY -m pytest \
  tests/gateway/test_session_scope_context.py \
  tests/tools/test_workflow_authority.py \
  tests/tools/test_registered_workflow_capability_policy.py \
  tests/tools/test_registered_local_workflow.py \
  tests/integration/test_registered_workflow_capabilities_no_live.py \
  -q -o addopts=
```

The listed integration file is the clean-baseline no-live smoke gate; no later discovery placeholder remains.

### Task R — Independent review and publication

For each repo:
- `git diff --check`
- `git status --short`
- independent security/privacy/spec review until PASS
- stage only listed task files
- commit conventional task commit
- push explicit feature branch to origin

## Concrete integration/restart gate

No restart is safe from the feature worktrees alone. Before the user's later restart, these exact active source roots must satisfy the following machine checks after `<CORE_FEATURE_SHA>` and `<OPS_FEATURE_SHA>` are replaced with the published commit IDs:

```bash
# Core active merge target
cd /Users/honbul/.hermes/hermes-agent
git merge-base --is-ancestor <CORE_FEATURE_SHA> HEAD
PY=/Users/honbul/.hermes/hermes-agent/venv/bin/python
unset PYTEST_ADDOPTS
$PY -m pytest \
  tests/gateway/test_session_scope_context.py \
  tests/tools/test_workflow_authority.py \
  tests/tools/test_registered_workflow_capability_policy.py \
  tests/tools/test_registered_local_workflow.py \
  tests/integration/test_registered_workflow_capabilities_no_live.py \
  -q -o addopts=
$PY -c 'from tools.registered_local_workflow import REGISTERED_LOCAL_WORKFLOW_SCHEMA as S; p=S["parameters"]; assert "social_conversation_record" in p["properties"]["action"]["enum"]; assert tuple(p["properties"]) == ("action","pending_id","reason_code","payload_name"); assert p["additionalProperties"] is False; print("core-social-schema-ok")'
# Expected final line: core-social-schema-ok

# Ops active merge target
cd /Users/honbul/.hermes/ops
git merge-base --is-ancestor <OPS_FEATURE_SHA> HEAD
unset PYTEST_ADDOPTS
$PY -m pytest \
  state/lifelog/tests/test_record_social_conversation.py \
  state/lifelog/tests/test_run_registered_recorder.py \
  -q -o addopts=
$PY -c 'import json; from pathlib import Path; d=json.loads(Path("state/lifelog/config/recorder-registry.json").read_text()); e=d["recorders"]["social_conversation.v1"]; assert e == {"script":"scripts/record_social_conversation.py","validator":"scripts/validate_lifelog.py","db":"lifelog.db","payload_root":".runtime-inputs/social-conversation","subject_allowlist":["person_park_sanghyun"],"confirmed_intent":"confirmed_social_conversation"}; print("ops-social-registry-ok")'
# Expected final line: ops-social-registry-ok
```

All commands must exit 0. This task publishes the two SHAs and exact commands but does not merge them into the active dirty roots, restart the gateway, or write canonical Lifelog data.

## Acceptance criteria

- Single trusted current Discord source including host-bound scope/guild.
- Live writer consumes the exact in-memory bytes read from a held verified fd; no payload pathname reopen.
- Commit-time authority/session/source/digest callback.
- Every persisted field and equality relation independently read back.
- Constant-safe output; valid bound runtime payload deleted on all owner outcomes; backup retained indefinitely unless separately approved.
- Required named tests and regressions green on both isolated branches.
- Independent review PASS.
- Two task-only commits pushed; active dirty roots and gateway untouched.
