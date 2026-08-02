---
schema: pkm-frontmatter/v1
document_id: semantic-authority-source-vertical-slice-20260801
title: "Semantic Authority Source Vertical Slice Plan"
created: "2026-08-01T14:13:00+09:00"
updated: "2026-08-01T14:24:12+09:00"
authors: [Hermes]
owners: [honbul]
status: draft
lifecycle: project
review_cadence: on_change
document_type: implementation-plan
audience: [honbul, Hermes]
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: high
summary: >-
  범용 정적분석기와 cron 전송 설계를 제외하고, 명시적 Company Work OS
  authority symbol 점검기와 initial-seed Fixer 하나를 실제 api_mode 및
  temp-owner no-live로 검증하는 첫 source vertical slice 계획.
tags:
  - hermes-agent
  - semantic-authority
  - watcher
  - fixer
  - company-work-os
projects: [hermes-agent]
areas: [agent-architecture, workflow-authority]
resources: []
relations:
  supersedes: []
  superseded_by: null
  related:
    - 2026-07-29-semantic-authority-watcher-fixer-vertical-slice-plan
review:
  cadence: on_change
  last_reviewed: null
  next_review: null
governance:
  pkm_required: true
  frontmatter_required: true
  approval_required_for_external_publish: true
automation:
  indexing: true
  extract_tasks: true
version: 0.2.0
---

# Semantic Authority Source Vertical Slice Implementation Plan

> **Status:** Draft. Plan/review only. Card `t_1a38445f` remains blocked. No implementation, actual provider call, production owner call, cron mutation, commit, push, merge, restart, or deployment is authorized by this document.

## Original Direction Preserved

This slice keeps the original product direction intact:

1. Open-language meaning and target selection belong to the main LLM.
2. Current-turn provenance, exact action/schema, owner, fixed target, privacy, replay, effect, and readback remain deterministic.
3. Watcher finds existing recognizers that cross that boundary.
4. Fixer changes one approved finding with RED → minimal GREEN → regression proof.
5. Watcher is intended to join the existing weekly Watchman later, but only after one manual run proves useful signal.
6. Material authority/data/privacy/core-flow risk matters; generic elegance and exhaustive static-analysis completeness do not.

## Why This Replaces the v0.3 Direction

Frozen v0.3 remains immutable review evidence at:

`docs/plans/2026-07-29-semantic-authority-watcher-fixer-vertical-slice-plan.md`

Its review loop expanded the first slice into a generic call/data-flow analyzer plus a delivery protocol. This plan removes both speculative layers. It implements only the concrete initial-seed boundary already observed in current source.

## Grill Summary

- **Target user:** Honbul operating Hermes Agent and registered local workflows.
- **Problem:** `company_work_os_initial_seed_record` is selected deterministically from two magic phrases even though semantic tool selection should belong to the main LLM.
- **Reference path:** `company_work_os_operating_record` already rejects raw-language grants and requires host-issued current foreground main-controller provenance.
- **Smallest proof:** one explicit-symbol Watcher finding, one initial-seed Fixer, one capture-only LLM-selection smoke, and one temp-owner no-live write/replay/readback proof.
- **Non-goals:** generic interprocedural graph, duplicate-rule discovery, automatic Fixer, weekly activation, external delivery adapter, new cron/daemon, all-recognizer migration.
- **Success:** the old initial-seed phrase grant disappears; main LLM can still select the closed action; trusted current-turn main-controller execution works only against temp owner; production evidence remains unchanged.
- **Stop condition:** any production defect outside the exact allowlist, production evidence drift, owner dispatch from the smoke, authority bypass, or dirty-tree overlap in the same hunks.

## DDD-light

### Ubiquitous Language

- **Semantic Selection:** main LLM chooses the closed tool action from ordinary language.
- **Deterministic Authority:** host-issued current-turn proof and fixed safety checks; it does not infer open-language meaning.
- **Explicit-symbol Watcher:** read-only AST inspection of named symbols and named relations only.
- **Finding:** one observed boundary violation with a fixed rule ID and fixer target.
- **Reference Surface:** a known compliant path used as a regression baseline, not a generic classification result.
- **Capture-only Smoke:** actual model/tool-selection path whose tool executor records the call but cannot dispatch an owner.
- **Temp-owner No-live:** real registered handler/policy/owner adapter path against isolated temp state with production evidence unchanged.

### Bounded Contexts

1. **Source Inspection:** fixed AST checks and stdout JSON.
2. **Semantic Selection:** actual/fake model selection with capture-only executor.
3. **Authority Enforcement:** host current-turn provenance and fixed action/target policy.
4. **Owner Execution:** temp Company Work OS owner, replay, effect, and readback.
5. **Activation:** explicitly deferred until this slice passes and the manual report is useful.

## Current Source Evidence

Observed on 2026-08-01:

- `tools/workflow_authority.py::_company_work_os_initial_seed_operation()` matches two exact phrases and returns `company_work_os_initial_seed_record`.
- `infer_explicit_workflow_grants()` calls that helper and returns the fixed initial-seed target grant.
- `infer_explicit_workflow_scope()` converts that grant to `trusted_local_record` and the fixed target.
- `infer_explicit_workflow_operations()` derives operations from the same grants.
- `tools/registered_local_workflow.py::_company_work_os_authority_mode()` currently requires `allowed_action_classes` and `allows_operation_target()` for initial-seed record.
- `_company_operating_authority_is_current()` already checks host-issued authority, session match, and `main_controller`.
- Existing operating-record raw-language authority tests passed in the prior inventory, and the current no-live fixture passed `2 passed` on 2026-08-01 with canonical main-controller/host authority context.
- The worktree is already dirty and contains unrelated active medication/lifelog changes. No global reset/stash/checkout is allowed.

## Policy Block

```yaml
policy_class: S3
execution_allowed: false
execution_approval_message_id: null
kanban_card: t_1a38445f
kanban_status: blocked
source_review_scope:
  - correctness
  - authority_bypass
  - wrong_target_or_owner_write
  - privacy_or_credential_exposure
  - replay_or_readback_failure
  - production_state_change
  - core_flow_regression
non_blocking_review_topics:
  - generic_static_analysis_completeness
  - future_rule_discovery
  - weekly_delivery_protocol
  - style_cleanup
  - unrelated_recognizer_migration
separate_approvals_required:
  - implementation_dispatch_and_card_unblock
  - actual_provider_credential_and_network_smoke
  - future_cron_update
  - future_cron_run
```

## Exact Source Allowlist

```text
scripts/audit_semantic_authority_boundaries.py
tests/scripts/test_audit_semantic_authority_boundaries.py
scripts/smoke_company_work_os_semantic_action_no_live.py
tests/scripts/test_smoke_company_work_os_semantic_action_no_live.py
tools/workflow_authority.py
tools/registered_local_workflow.py
tests/tools/test_workflow_authority.py
tests/tools/test_registered_local_workflow.py
tests/integration/test_company_work_os_initial_seed_no_live.py
```

`tests/integration/test_company_work_os_operating_record_no_live.py` is a read-only regression path in this slice, not an edit target.

No edit to `agent/turn_context.py` is planned. If implementation proves it necessary, stop and replan before touching it.

## Task 0: Seal the Dirty-tree Baseline Without New Artifacts

1. Record current branch/status and path-scoped diffs for the exact allowlist.
2. Record SHA-256 for each existing allowlisted file and `absent` for planned-create files.
3. Do not copy source into `/tmp`; do not create a rollback authority file.
4. Snapshot production Company Work OS DB/WAL/SHM, seed artifact, operating input root, and operating lock using the existing no-live test evidence helpers; do not print contents.
5. Run current focused tests and preserve direct terminal output in the session.
6. If another task changes the same planned hunk before implementation, stop. Do not overwrite or merge it automatically.

Baseline commands:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest \
  tests/tools/test_workflow_authority.py \
  tests/tools/test_registered_local_workflow.py \
  tests/integration/test_company_work_os_initial_seed_no_live.py \
  tests/integration/test_company_work_os_operating_record_no_live.py \
  -q -o 'addopts='
```

Known pre-plan failures must be recorded separately from regressions.

## Task 1: Build the Explicit-symbol Watcher

**Create:**

- `scripts/audit_semantic_authority_boundaries.py`
- `tests/scripts/test_audit_semantic_authority_boundaries.py`

### Fixed checks only

The Watcher parses, but never imports or executes, these files:

- `tools/workflow_authority.py`
- `tools/registered_local_workflow.py`

It performs exactly four named checks. Each initial-seed check has three deterministic states: `finding` for the exact legacy shape, `compliant` for the exact post-fix shape, and `shape_changed` for any partial or unexpected state.

1. `initial_seed_phrase_grant`
   - `finding`: `_company_work_os_initial_seed_operation` exists, contains `re.fullmatch` plus the literal operation, and `infer_explicit_workflow_grants` directly calls it and returns the canonical fixed target.
   - `compliant`: the helper is absent and `infer_explicit_workflow_grants` contains neither its call nor an initial-seed operation/target literal.
   - Current state: `finding`.
2. `initial_seed_grant_propagation`
   - `finding`: `infer_explicit_workflow_scope` maps the operation to `trusted_local_record`/fixed target and `infer_explicit_workflow_operations` derives it from grants.
   - `compliant`: those initial-seed-specific class/target branches are absent and the operations function contains no separate initial-seed special case.
   - This check contributes to the same finding, never a second one.
3. `operating_record_no_language_grant`
   - `compliant`: `infer_explicit_workflow_grants` contains no `company_work_os_operating_record` literal, while `_company_operating_authority_is_current` calls `is_host_issued_current_turn_authority`, `_authority_matches_session`, and `get_session_controller_role` and contains literal `main_controller`.
   - Any other state is `shape_changed`; it is never an initial-seed finding.
4. `initial_seed_owner_still_phrase_bound`
   - `finding`: the initial-seed record branch in `_company_work_os_authority_mode` requires `trusted_local_record` and `allows_operation_target` without host-issued/main-controller checks.
   - `compliant`: that branch instead requires `is_host_issued_current_turn_authority`, session/current source, and literal `main_controller`, and no longer requires a raw-language-derived operation grant.

Exactly the complete legacy state yields one `initial_seed_phrase_grant` high finding. Exactly the complete post-fix state yields zero findings and exit `0`. A mixed legacy/post-fix state, missing reference symbol, parse failure, or any unrecognized AST shape yields `shape_changed` and exit `2`; the Watcher never guesses or scans arbitrary calls.

### Output contract

- stdout only; no output path option and no files.
- schema: `semantic-authority-explicit-watch/v1`.
- exact top-level keys: `schema,ok,source_sha256,checks,findings`.
- check fields: `check_id,status=finding|compliant|shape_changed,symbols`.
- finding fields: `finding_id,severity,rule_id,fixer_target,check_ids`.
- `ok` means structurally valid report with no `shape_changed`; valid findings do not make `ok=false`.
- identifiers are fixed enums or repo-relative qualified symbols; no source snippets, regex literals, prompts, credentials, exception text, absolute paths, session/channel/user IDs, or free-form evidence.
- deterministic sorting by `check_id` and `finding_id`.
- exit `0` for structurally valid pre-fix or post-fix report; exit `2` for parse/schema/shape errors. Findings are not process errors.

### RED/GREEN tests

1. Complete legacy fixture emits exactly one `initial_seed_phrase_grant` high finding and exit `0`.
2. Complete post-fix fixture emits four compliant checks, zero findings, and exit `0`.
3. Partial removal of helper, call, propagation, or owner binding yields `shape_changed` and exit `2`.
4. Operating reference is compliant and non-actionable.
5. Validators/other recognizers are not inventoried or promoted.
6. Missing/renamed operating reference symbol yields `shape_changed`, not a false clean.
7. Same source bytes produce identical JSON; no timestamp exists.
8. Serialized output contains none of the injected secret/private sentinel values.

Manual proof:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python scripts/audit_semantic_authority_boundaries.py | venv/bin/python -m json.tool
```

## Task 2: Build Capture-only Semantic-selection Smoke

**Create:**

- `scripts/smoke_company_work_os_semantic_action_no_live.py`
- `tests/scripts/test_smoke_company_work_os_semantic_action_no_live.py`

### Scope

- Model-visible schema contains only `registered_local_workflow` with action enum containing only `company_work_os_initial_seed_record` and no additional args.
- A fresh `AIAgent` is constructed for every case.
- Before the model call, instance `_execute_tool_calls` is replaced with a collector returning a synthetic no-write receipt.
- `run_agent.handle_function_call` and the effective registry handler are fail sentinels.
- Fake mode uses provider-shaped fake events and fail sentinels for config credential resolution and socket creation.
- Actual mode uses the configured `openai-codex` runtime and is a separately approved credential/network action.
- stdout only; no report path and no files.
- output schema contains only mode/provider/api_mode, pass boolean, per-case selected action count, and closed error code. It excludes prompts, responses, credentials, paths, private IDs, and exception text.

### Cases

- Positive direct instruction → exactly one initial-seed record action.
- Negation → zero actions.
- Question → zero actions.
- Plan/explanation → zero actions.
- Quoted/reported instruction → zero actions.

### Gate

- Fake mode must prove zero credential, network, and production dispatch calls.
- Actual mode must produce one positive and zero negative selections across fresh sessions.
- Any production sentinel, negative selection, malformed output, or provider failure stops before Fixer GREEN.
- Positive miss permits adjustment only to the reviewed model-visible tool description; phrase grants must not return.

## Task 3: Verify the Operating Reference Fixture

**Read-only path:** `tests/integration/test_company_work_os_operating_record_no_live.py`.

- Current evidence on 2026-08-01 is `2 passed`; no edit is planned.
- Re-run it before and after the source slice to preserve host-issued authority, session/source binding, `main_controller`, temp owner, production evidence, and leaf/background/stale denial behavior.
- If it fails before implementation, classify the failure as baseline drift and stop. If it fails only after task-owned changes, treat it as a regression and repair only the task-owned cause.
- Do not modify operating-record production code or its test merely to accommodate initial-seed behavior.

## Task 4: Fix `initial_seed_phrase_grant`

### RED

Update/add tests to require:

1. Exact old English/Korean magic phrases mint no initial-seed operation, class, or target grant.
2. Ordinary variants, negation, questions, plans, and quoted text also mint none.
3. A host-issued current foreground `main_controller` authority can execute the exact closed initial-seed record action without any raw-language-derived operation grant.
4. Forged/unsealed, stale, wrong-session, non-main-controller, background, cron, delegate, review, subagent, and webhook contexts deny with zero owner calls.
5. Preview remains local-read/current-turn according to its existing contract.
6. Unknown action or extra tool args deny schema before owner.
7. Temp owner insert → replay no-op/readback remains correct and production evidence is unchanged.

### Minimal GREEN

- Remove `_company_work_os_initial_seed_operation`.
- Remove its branch from `infer_explicit_workflow_grants`.
- Remove initial-seed class/target derivation that is reachable only from that grant.
- Change the initial-seed record branch in `_company_work_os_authority_mode` to require:
  - non-blocked foreground platform,
  - current authority with source event and session match,
  - `is_host_issued_current_turn_authority(authority)`,
  - `get_session_controller_role() == "main_controller"`,
  - exact closed action and existing fixed owner target/policy.
- Do not broaden caller-controlled target or payload input.
- Do not change operating-record or team-roster behavior in this slice.

If existing shared helpers cannot express this without modifying `agent/turn_context.py`, stop and replan.

## Task 5: Verification and Manual Watcher Evaluation

Run:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest \
  tests/scripts/test_audit_semantic_authority_boundaries.py \
  tests/scripts/test_smoke_company_work_os_semantic_action_no_live.py \
  tests/tools/test_workflow_authority.py \
  tests/tools/test_registered_local_workflow.py \
  tests/integration/test_company_work_os_initial_seed_no_live.py \
  tests/integration/test_company_work_os_operating_record_no_live.py \
  -q -o 'addopts='
venv/bin/python -m py_compile \
  scripts/audit_semantic_authority_boundaries.py \
  scripts/smoke_company_work_os_semantic_action_no_live.py \
  tools/workflow_authority.py \
  tools/registered_local_workflow.py
venv/bin/python scripts/audit_semantic_authority_boundaries.py
```

Then verify:

- pre-fix fixture finds exactly one actionable initial-seed finding;
- post-fix current source reports no actionable initial-seed phrase finding;
- operating reference remains compliant;
- no unrelated regex/parser is promoted;
- fake capture-only smoke passes with zero credential/network/owner calls;
- separately approved actual smoke passes;
- initial-seed temp owner insert/replay/readback passes;
- production evidence is unchanged;
- final diff contains only exact allowlist paths and no unrelated dirty hunks.

## Activation Decision Gate

Only after Task 5 evidence exists, propose a separate activation plan if all are true:

1. Watcher found the intended pre-fix defect.
2. It produced no false high finding on operating reference or unrelated validators.
3. Post-fix report became clean for the exact rule.
4. Actual main-LLM tool selection passed.
5. Temp-owner no-live and production-invariance checks passed.
6. The report was useful enough to decide a real Fixer action.

The future activation plan may update only existing job `ffa3629fc41e`; it must not create a new cron/daemon or auto-run Fixer. No cron design or external delivery protocol is part of this source slice.

## Review Gate

One bounded checkpoint wave reviews this plan. Reviewers must distinguish implementation verification from plan blockers.

A plan blocker is limited to:

- authority bypass or acceptance of model/retrieved text as authority;
- caller-controlled wrong target/owner write;
- credential/private data exposure;
- replay/effect/readback contract loss;
- production state mutation during no-live proof;
- core initial-seed or operating flow regression;
- an exact named task that cannot be executed as written.

The following are explicitly non-blocking for this slice:

- lack of generic call/data-flow analysis;
- inability to discover unknown recognizers;
- future weekly delivery details;
- all-recognizer migration completeness;
- formatting/style/generalization improvements.

If consequential blockers remain after one revision, stop for owner judgment rather than expanding the architecture.

## Acceptance Criteria

1. Explicit-symbol Watcher is read-only, stdout-only, deterministic, and limited to four named checks.
2. It reports the current initial-seed phrase grant exactly once before the fix.
3. Initial-seed raw language no longer mints deterministic record authority after the fix.
4. Main LLM capture-only smoke can select the exact closed action without production dispatch.
5. Deterministic owner execution requires host-issued current foreground main-controller provenance.
6. Forged/stale/background/cron/delegate/review/subagent/webhook paths deny.
7. Temp-owner insert/replay/readback passes and production evidence is unchanged.
8. Operating-record reference behavior remains intact.
9. No unrelated recognizer, cron, external delivery, card state, commit/push/merge, restart, or production owner is changed.
10. Weekly activation remains a separate evidence-gated plan.

## Rollback

- No automatic rollback script or baseline copy is created.
- For created files, delete only if current bytes still equal the task-final SHA and no other task modified them.
- For modified files, reverse only task-owned hunks after exact diff review; never reset/checkout the dirty worktree.
- Re-run baseline focused tests and production evidence checks after rollback.
- If same-hunk concurrent changes prevent a clean inverse patch, stop and perform a reviewed manual merge; do not overwrite.

## Handoff

- Plan path: `docs/plans/2026-08-01-semantic-authority-source-vertical-slice-plan.md`
- Frozen v0.3 review document remains unchanged.
- Card `t_1a38445f` remains blocked.
- Implementation requires separate explicit dispatch/unblock approval after this plan passes review.
- Actual provider smoke and future cron activation each require their own current-turn approval.
