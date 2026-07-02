---
schema: pkm-frontmatter/v1
document_id: "kanban-status-memory-hardening-plan-20260702"
title: "Kanban Status-Memory Approval Boundary Hardening Plan"
subtitle: "기존 Kanban 카드 상태저장 메타데이터를 추가 승인 없이 기록하도록 만드는 Hermes 구현 계획"
created: "2026-07-02T11:16:45+09:00"
updated: "2026-07-02T11:29:30+09:00"
authors:
  - Hermes
owners:
  - honbul
status: review
lifecycle: draft
document_type: plan
audience:
  - honbul
  - Hermes implementers
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: high
summary: >-
  Hermes가 기존/승인된 Kanban 카드에 repo URL, PR URL, artifact link, 진행상태, 검증요약 같은 상태저장 메타데이터를 기록할 때 매번 사용자 승인을 요구하지 않도록 runtime-live prompt와 Kanban tool contract를 고치는 구현 계획이다. 새 카드 생성, dispatch/unblock/complete/archive/delete, status/assignee/priority 변경, 공개/파괴적 실행은 계속 승인 게이트에 남긴다.
tags:
  - hermes-agent
  - kanban
  - runtime-live-guard
  - writing-plan
  - status-memory
aliases:
  - Kanban status memory hardening
  - 기존 카드 상태저장 승인 경계 개선
projects:
  - Hermes Agent
areas:
  - agent-ops
  - software-development
resources:
  - writing-plans
  - grill-me
  - kanban-status-memory-hardening
entities:
  people:
    - honbul
  organizations:
    - Nous Research
  brands:
    - Hermes
  products:
    - Hermes Agent
  systems:
    - Kanban
    - Runtime-live action guard
sources:
  - type: user_request
    title: "Kanban 상태저장 업데이트를 승인 없이 자동 기록하도록 개선 방향성과 구현 계획 작성 요청"
    url: null
    date: 2026-07-02
links:
  canonical: /Users/honbul/.hermes/hermes-agent/docs/plans/2026-07-02-kanban-status-memory-hardening-plan.md
  source: null
  related:
    - /Users/honbul/.hermes/hermes-agent/gateway/kanban_intake.py
    - /Users/honbul/.hermes/hermes-agent/tools/kanban_tools.py
    - /Users/honbul/.hermes/hermes-agent/agent/prompt_builder.py
    - /Users/honbul/.hermes/hermes-agent/tools/delegate_tool.py
relations:
  parent: null
  children: []
  depends_on:
    - /Users/honbul/.hermes/docs/First Landing Package.md
    - /Users/honbul/.hermes/skills/software-development/software-development-lifecycle-operations/references/kanban-status-memory-hardening.md
  supersedes: []
  superseded_by: null
review:
  cadence: on_change
  last_reviewed: null
  next_review: null
governance:
  pkm_required: true
  frontmatter_required: true
  approval_required_for_external_publish: true
origin:
  type: agent
  trigger: user_request
  actor: Hermes
artifact:
  role: primary
  default_visibility: show
  retention: keep
  regeneration: manual
automation:
  indexing: true
  extract_tasks: true
  sync_targets:
    - local_markdown
version: 0.1.0
---

# Kanban Status-Memory Approval Boundary Hardening Plan

> **For Hermes:** Use `subagent-driven-development` or equivalent task-by-task TDD to implement this plan only after review PASS and explicit implementation approval.

**Goal:** 기존/승인된 Kanban 카드에 상태저장 메타데이터를 기록할 때 Hermes가 매번 사용자에게 승인을 묻지 않고 바로 기록하도록 runtime-live guard와 Kanban tool contract를 정렬한다.

**Architecture:** 1차 구현은 prompt/tool-contract hardening이다. `RUNTIME_LIVE_ENFORCEMENT_GUIDANCE`와 delegate child prompt에 기존 카드 status-memory 예외를 명시하고, `kanban_comment` tool schema에 repo/PR/artifact/progress/verification note를 즉시 기록해야 한다는 계약을 추가한다. 추후 진짜 hard enforcement가 필요하면 central action taxonomy를 별도 phase로 설계한다.

**Tech Stack:** Python, pytest, Hermes Agent gateway/runtime prompt builder, Kanban SQLite task board, model tool schema registry.

---

## Policy Block

```yaml
risk_class: S2
policy_source: /Users/honbul/.hermes/docs/hermes-autonomous-recursive-improvement-policy-20260429.md
required_reviews:
  - plan-eng-review
execution_allowed: false
execution_scope: plan_only_until_review_pass_and_user_approval
measurement_source: focused_pytest_bundle_and_prompt_schema_assertions
public_surface_policy: none
forbidden_without_confirmation:
  - implementation_code_change
  - git_push
  - gateway_restart
  - public_deploy
  - cron_mutation
  - credential_read
  - live_db_migration
  - destructive_delete
```

## Kanban Tracking

- Tracking card: `t_7a04b8e8`
- Board: `default`
- Tenant: `hermes-agent`
- Card policy for this work: comments/progress notes on this existing card are **status-memory updates** and should be recorded directly. Implementation, push, restart, deploy, DB migration, or destructive actions remain approval-gated.

---

## Grill Summary

- **Confirmed goal:** Hermes Kanban 상태저장 UX를 코드/프롬프트 레벨에서 고쳐, 기존/승인된 카드에 repo URL·PR URL·artifact link·진행상태·검증요약을 기록할 때 추가 승인을 묻지 않게 한다.
- **Target user / actor:** Discord/CLI에서 Hermes를 쓰는 honbul, Kanban worker/orchestrator/subagent, Hermes main agent.
- **Current state checked:**
  - `agent/prompt_builder.py`의 `RUNTIME_LIVE_ENFORCEMENT_GUIDANCE`가 `state/registry mutation`을 넓게 승인 대상으로 묶는다.
  - `agent/system_prompt.py`는 해당 guard를 tool-capable agent system prompt에 주입한다.
  - `tools/delegate_tool.py`는 child/subagent prompt에 별도 runtime-live guard를 주입한다.
  - `tools/kanban_tools.py`의 `kanban_comment`는 상태저장 메타데이터 예외를 명시하지 않는다.
  - `gateway/kanban_intake.py`에는 기존 카드 업데이트 intent를 새 카드 후보로 만들지 않는 suppression 로직이 이미 있다.
  - `tests/gateway/test_kanban_intake_detector.py` + `tests/gateway/test_kanban_intake_post_turn.py`는 현재 66 passed.
- **Constraints:** prompt caching을 깨지 말 것; 새 core tool 추가 금지; Kanban metadata 예외를 실제 실행/공개/파괴적 mutation까지 넓히지 말 것; delegate prompt도 같이 고칠 것.
- **Non-goals:** 이번 phase에서 실제 코드 구현/배포/게이트웨이 재시작/DB migration/새 hard sandbox 구현은 하지 않는다. 새 카드 자동 생성 정책도 바꾸지 않는다.
- **Success criteria:** focused tests가 통과하고, prompt/schema tests가 “기존 카드 status-memory metadata는 승인 예외”와 “새 work/action mutation은 승인 필요”를 동시에 검증한다.
- **Failure criteria:** 모델이 여전히 `repo URL 카드에 기록할까요?`처럼 기존 카드 metadata 기록을 승인 요청으로 처리하거나, 반대로 unblock/complete/archive/delete/public deploy까지 승인 예외로 오해하게 만드는 경우.
- **Domain terms / Ubiquitous Language candidates:** 아래 DDD Light 참조.
- **Open questions:** 없음. Hard taxonomy는 이번 plan의 Phase 2로만 남긴다.
- **Assumptions accepted:** 사용자의 현재 메시지는 plan/card/review/status-memory workflow 수행 승인이다. Implementation code change와 git push는 별도 승인 전까지 잠근다.

## DDD Light

### Bounded Context

- **Context name:** Kanban status-memory approval boundary
- **Boundary:** Hermes agent prompt, delegate child prompt, Kanban model tool schema, Kanban intake suppression tests
- **Out of boundary:** public deploy, gateway restart, live DB migration, cron mutation, actual implementation execution, general memory preference storage

### Ubiquitous Language

| Term | Meaning | Do Not Confuse With | Example |
|---|---|---|---|
| Existing/approved card | 이미 생성되어 있거나 승인된 작업 흐름의 Kanban card | 새 카드 후보 또는 신규 작업 승인 | `t_7a04b8e8`에 plan path 기록 |
| Status-memory metadata | 기존 카드에 붙이는 상태저장용 근거/링크/진행 노트 | 작업 실행 결정 또는 상태 전환 | repo URL, PR URL, artifact path, test summary |
| Live action mutation | 시스템/서비스/외부 상태를 실행적으로 바꾸는 작업 | 기존 카드 comment 추가 | deploy, cron resume, DB migration, unblock/complete |
| Approval-free metadata boundary | 추가 승인 없이 바로 기록해도 되는 좁은 예외 | 모든 Kanban mutation 승인 면제 | `kanban_comment`로 검증 요약 남기기 |
| Hard taxonomy | tool/action을 기계적으로 분류하는 정책 레이어 | prompt wording만 바꾸는 soft guard | `status_memory_allowed`, `approval_required_live_mutation` |

### Core Scenarios

- **Scenario 1:** Hermes가 GitHub repo/PR URL을 만들거나 발견했다. 이미 추적 카드가 있다면 `kanban_comment`로 즉시 링크를 기록한다.
- **Scenario 2:** Worker가 focused tests를 돌렸다. 기존 카드에 검증 요약을 즉시 남긴다.
- **Scenario 3:** Worker가 card를 `done`으로 complete하거나 unblock하려 한다. 이는 상태저장 metadata가 아니므로 기존 runtime-live approval policy를 따른다.
- **Scenario 4:** Conversational intake가 `t_<hex> 카드 업데이트 승인` 같은 메시지를 본다. 새 카드 후보를 만들지 않고 existing-card update intent로 suppress한다.

### Invariants

- 기존 카드에 comment/progress/link/verification note를 붙이는 것은 승인-free status-memory metadata다.
- 새 카드 생성, dispatch/unblock/complete/archive/delete, assignee/priority/status 변경은 status-memory metadata가 아니다.
- Prompt exception은 main agent와 delegate child prompt 모두에 존재해야 한다.
- Assistant summary는 mutation approval을 만들어낼 수 없다. User/current task scope가 기준이다.
- 변경은 prompt caching 안정성을 깨지 않는 stable prompt text 수정이어야 한다.

### Non-goals

- 새 Kanban core tool 추가하지 않음.
- runtime-live guard 전체 비활성화하지 않음.
- Kanban status transition을 자동화하지 않음.
- dashboard API 권한/인증 체계를 바꾸지 않음.

---

## Verified Context Inventory

| Source | Checked fact | Plan implication |
|---|---|---|
| `agent/prompt_builder.py:331-345` | Runtime-live guard가 `state/registry mutation`을 broad live action으로 문구화한다. | 이 문구에 Kanban status-memory exception을 추가해야 한다. |
| `agent/system_prompt.py:189-196` | Guard는 tool-capable agents의 stable prompt에 주입된다. | prompt test로 main-agent injection을 검증한다. |
| `tools/delegate_tool.py:697-707` | Child/subagent prompt에도 별도 live-action boundary가 들어간다. | delegate prompt도 같은 예외를 포함해야 한다. |
| `tools/kanban_tools.py:1279-1304` | `kanban_comment` schema는 durable note 용도만 설명하고 approval-free metadata boundary는 없다. | tool schema contract를 강화한다. |
| `hermes_cli/kanban.py:516-523`, `1838-1850` | CLI comment path가 `kb.add_comment`를 호출한다. | persistence는 이미 존재하므로 새 저장 경로는 필요 없다. |
| `hermes_cli/kanban_db.py:2933-2952` | `add_comment`는 existing task check 후 comment/event를 저장한다. | status-memory 저장은 기존 DB function으로 충분하다. |
| `gateway/kanban_intake.py:797-810`, `836-853` | existing-card update intent suppression이 있다. | 새 카드 후보 suppression은 유지/회귀 테스트한다. |
| `tests/agent/test_system_prompt.py:104-128` | runtime-live prompt injection/disable tests가 있다. | exception wording assertion을 추가한다. |
| `tests/tools/test_delegate.py:2730-2740` | child prompt runtime-live boundary test가 있다. | child exception assertion을 추가한다. |
| `tests/tools/test_kanban_tools.py` | tool visibility/schema tests가 있다. | `kanban_comment` schema text assertion을 추가한다. |

---

## Implementation Plan

### Task 1: Add RED tests for main-agent runtime-live exception wording

**Objective:** Main system prompt가 existing Kanban card status-memory metadata를 approval-free exception으로 설명하는지 테스트한다.

**Files:**
- Modify: `tests/agent/test_system_prompt.py`
- Later modify: `agent/prompt_builder.py`

**Step 1: Write failing test**

Add assertions to `TestRuntimeLiveEnforcement.test_runtime_live_guard_injected_for_tool_capable_agents`:

```python
expected_allowed_fragments = [
    "Existing Kanban card status-memory",
    "comments, progress notes, repo/PR/artifact links",
    "verification summaries",
    "handoff notes",
    "does not require separate approval",
]
expected_forbidden_fragments = [
    "create new work",
    "dispatch/unblock/complete/archive/delete",
    "change status/assignee/priority",
    "publish externally",
    "read credentials",
    "run migrations",
    "destructive actions",
]
for fragment in expected_allowed_fragments + expected_forbidden_fragments:
    assert fragment in stable
```

**Step 2: Run RED**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest tests/agent/test_system_prompt.py::TestRuntimeLiveEnforcement::test_runtime_live_guard_injected_for_tool_capable_agents -q -o 'addopts='
```

Expected: FAIL because the current prompt does not include the exception wording.

### Task 2: Patch main runtime-live guard wording narrowly

**Objective:** `RUNTIME_LIVE_ENFORCEMENT_GUIDANCE`에 Kanban status-memory exception을 추가하되 approval gate의 핵심은 유지한다.

**Files:**
- Modify: `agent/prompt_builder.py:331-345`

**Step 1: Minimal implementation**

Patch the guidance after the live-action examples:

```python
"Existing Kanban card status-memory metadata — comments, progress notes, "
"repo/PR/artifact links, verification summaries, and handoff notes attached "
"to an existing or already-approved task — does not require separate approval "
"when it does not create new work, dispatch/unblock/complete/archive/delete a "
"task, change status/assignee/priority, publish externally, read credentials, "
"run migrations, or perform destructive actions.\n"
```

**Step 2: Run GREEN**

```bash
venv/bin/python -m pytest tests/agent/test_system_prompt.py::TestRuntimeLiveEnforcement -q -o 'addopts='
```

Expected: PASS.

### Task 3: Add RED tests for delegate child prompt exception wording

**Objective:** Subagents가 같은 status-memory exception을 받도록 보장한다.

**Files:**
- Modify: `tests/tools/test_delegate.py`
- Later modify: `tools/delegate_tool.py`

**Step 1: Write failing test**

Extend `TestChildSystemPromptRuntimeLiveEnforcement.test_child_prompt_includes_live_action_boundary`:

```python
expected_allowed_fragments = [
    "Existing Kanban card status-memory",
    "comments, progress notes, repo/PR/artifact links",
    "verification summaries",
    "handoff notes",
    "does not require separate approval",
]
expected_forbidden_fragments = [
    "create new work",
    "dispatch/unblock/complete/archive/delete",
    "change status/assignee/priority",
    "publish externally",
    "read credentials",
    "run migrations",
    "destructive actions",
]
for fragment in expected_allowed_fragments + expected_forbidden_fragments:
    self.assertIn(fragment, prompt)
```

**Step 2: Run RED**

```bash
venv/bin/python -m pytest tests/tools/test_delegate.py::TestChildSystemPromptRuntimeLiveEnforcement::test_child_prompt_includes_live_action_boundary -q -o 'addopts='
```

Expected: FAIL.

### Task 4: Patch delegate runtime-live guard wording

**Objective:** Child/subagent prompt에서도 existing-card metadata exception을 명시한다.

**Files:**
- Modify: `tools/delegate_tool.py:697-707`

**Step 1: Minimal implementation**

Add the same narrow exception to `_build_child_system_prompt` runtime-live section. Keep wording aligned with `agent/prompt_builder.py`, but adapt “latest user message” to child context:

```python
"Existing Kanban card status-memory metadata — comments, progress notes, "
"repo/PR/artifact links, verification summaries, and handoff notes attached "
"to an existing or already-approved task — does not require separate approval "
"when it does not create new work, dispatch/unblock/complete/archive/delete a "
"task, change status/assignee/priority, publish externally, read credentials, "
"run migrations, or perform destructive actions. "
```

**Step 2: Run GREEN**

```bash
venv/bin/python -m pytest tests/tools/test_delegate.py::TestChildSystemPromptRuntimeLiveEnforcement -q -o 'addopts='
```

Expected: PASS.

### Task 5: Add RED test for `kanban_comment` schema contract

**Objective:** model-facing Kanban tool schema가 repo/PR/artifact/progress/verification notes를 direct status-memory update로 설명하는지 검증한다.

**Files:**
- Modify: `tests/tools/test_kanban_tools.py`
- Later modify: `tools/kanban_tools.py`

**Step 1: Write failing test**

Add near schema/gating tests:

```python
def test_kanban_comment_schema_marks_existing_card_metadata_approval_free():
    from tools.kanban_tools import KANBAN_COMMENT_SCHEMA

    desc = KANBAN_COMMENT_SCHEMA["description"]
    required_fragments = [
        "existing task",
        "status-memory",
        "repo/PR/artifact",
        "do not ask for separate approval",
        "creating new work",
        "dispatching, unblocking, completing, archiving, or deleting",
        "changing status/assignee/priority",
        "public delivery/deploy",
        "credential reads",
        "DB migrations",
        "destructive actions",
    ]
    for fragment in required_fragments:
        assert fragment in desc
```

**Step 2: Run RED**

```bash
venv/bin/python -m pytest tests/tools/test_kanban_tools.py::test_kanban_comment_schema_marks_existing_card_metadata_approval_free -q -o 'addopts='
```

Expected: FAIL.

### Task 6: Patch `kanban_comment` schema wording

**Objective:** `kanban_comment` tool을 기존 카드 상태저장 메타데이터의 기본 기록 수단으로 정의한다.

**Files:**
- Modify: `tools/kanban_tools.py:1279-1304`

**Step 1: Minimal implementation**

Replace/extend `KANBAN_COMMENT_SCHEMA["description"]` with wording like:

```python
"Append a comment to an existing task's thread. Use this directly — do not "
"ask for separate approval — for status-memory metadata that should outlive "
"this run: progress notes, repo/PR/artifact links, verification summaries, "
"handoff notes, and rationale attached to an existing or already-approved "
"task. This tool is not for creating new work, dispatching, unblocking, "
"completing, archiving, or deleting tasks; changing status/assignee/priority; "
"public delivery/deploy; credential reads; DB migrations; or destructive "
"actions. Those actions follow the runtime-live approval boundary."
```

**Step 2: Run GREEN**

```bash
venv/bin/python -m pytest tests/tools/test_kanban_tools.py::test_kanban_comment_schema_marks_existing_card_metadata_approval_free -q -o 'addopts='
```

Expected: PASS.

### Task 7: Add explicit existing-card metadata intake suppression tests

**Objective:** Conversational intake가 repo URL, PR URL, artifact link, verification summary, handoff note 같은 기존 카드 상태저장 메타데이터 요청을 새 카드 후보로 만들지 않는지 명시적으로 검증한다.

**Files:**
- Modify: `tests/gateway/test_kanban_intake_detector.py`
- Modify: `tests/gateway/test_kanban_intake_post_turn.py`
- Modify only if RED tests fail: `gateway/kanban_intake.py`

**Step 1: Add parametrized detector RED tests**

Add cases similar to the existing `existing_card_update_intent` coverage:

```python
@pytest.mark.parametrize("user_text", [
    "t_deadbeef 카드에 repo URL https://github.com/NousResearch/hermes-agent 기록해줘",
    "t_deadbeef 카드에 PR URL https://github.com/NousResearch/hermes-agent/pull/123 남겨",
    "t_deadbeef 카드에 artifact link /tmp/report.pdf 기록 남겨",
    "t_deadbeef 카드에 verification summary: focused tests 5 passed 기록",
    "t_deadbeef 카드에 handoff note: 다음 worker는 prompt_builder.py부터 보면 됨 남겨",
])
def test_existing_card_metadata_updates_suppressed(user_text):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_test",
        user_summary=user_text,
        assistant_summary="Hermes Agent repo follow-up implementation work exists.",
        default_board="default",
        default_tenant="hermes-agent",
    )
    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "existing_card_update_intent"
    assert KeywordHeuristicDetector().detect(request).card_worthy is False
```

Expected initial result: if current regex already covers all phrases, tests may pass immediately; if not, this is the RED signal to patch `gateway/kanban_intake.py` narrowly.

**Step 2: Add post-turn fake-detector suppression tests**

In `tests/gateway/test_kanban_intake_post_turn.py`, add a parametrized async test where the fake detector returns `DetectorDecision(card_worthy=True, title="Existing card metadata update")` but `_maybe_build_kanban_intake_proposal_message(...)` returns `None` for the same five phrases. This proves assistant summaries or detector positives cannot forge a new-card proposal for existing-card metadata updates.

**Step 3: Patch intake regex only if needed**

If RED tests fail, extend `_EXISTING_CARD_UPDATE_VERB_RE` or a dedicated helper in `gateway/kanban_intake.py` to include narrow metadata verbs/nouns:

```text
repo URL, PR URL, artifact link, verification summary, handoff note,
링크 기록, URL 기록, 검증 요약, 인계 노트
```

Do not treat generic `repo`, `PR`, or `artifact` mentions without an existing-card anchor as suppression.

**Step 4: Run focused intake coverage**

```bash
venv/bin/python -m pytest \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  -q -o 'addopts='
```

Expected: PASS. Current baseline before this plan: `66 passed, 1 warning in 0.68s`.

### Task 8: Run full focused regression bundle

**Objective:** Main prompt, delegate prompt, Kanban tool schema, and intake suppression all pass together.

**Files:**
- All modified files from Tasks 1-7.

**Step 1: Run bundle**

```bash
venv/bin/python -m pytest \
  tests/agent/test_system_prompt.py \
  tests/tools/test_delegate.py \
  tests/tools/test_kanban_tools.py \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  -q -o 'addopts='
```

Expected: PASS.

### Task 9: Static checks and diff hygiene

**Objective:** Prompt strings/schema edits have no syntax or whitespace issues.

**Files:**
- `agent/prompt_builder.py`
- `tools/delegate_tool.py`
- `tools/kanban_tools.py`
- `tests/agent/test_system_prompt.py`
- `tests/tools/test_delegate.py`
- `tests/tools/test_kanban_tools.py`
- `tests/gateway/test_kanban_intake_detector.py`
- `tests/gateway/test_kanban_intake_post_turn.py`
- `gateway/kanban_intake.py` if Task 7 requires production regex changes

**Step 1: Compile changed Python files**

```bash
venv/bin/python -m py_compile \
  agent/prompt_builder.py \
  tools/delegate_tool.py \
  tools/kanban_tools.py \
  tests/agent/test_system_prompt.py \
  tests/tools/test_delegate.py \
  tests/tools/test_kanban_tools.py \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  gateway/kanban_intake.py
```

Expected: no output / exit 0.

**Step 2: Check whitespace**

```bash
git diff --check -- \
  agent/prompt_builder.py \
  tools/delegate_tool.py \
  tools/kanban_tools.py \
  tests/agent/test_system_prompt.py \
  tests/tools/test_delegate.py \
  tests/tools/test_kanban_tools.py \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  gateway/kanban_intake.py
```

Expected: no output / exit 0.

### Task 10: Optional Phase 2 design spike for hard action taxonomy

**Objective:** If prompt/tool-contract hardening is insufficient, design a real central policy taxonomy without overbuilding it into this patch.

**Files:**
- Create only if explicitly approved later: `agent/action_policy.py` or equivalent
- Tests only if explicitly approved later: `tests/agent/test_action_policy.py`

**Spike questions:**

- Can Kanban tool handlers expose machine-readable action classes such as `status_memory_allowed`, `approval_required_workflow_mutation`, and `destructive_or_public`?
- Should a hard guard live in the model tool dispatcher, in individual tool handlers, or in a future runtime policy wrapper?
- How can this avoid blocking legitimate worker lifecycle calls already scoped by `HERMES_KANBAN_TASK`?

**Exit criteria:** one ADR or follow-up plan, not speculative production code.

---

## Acceptance Criteria

- [ ] Main runtime-live prompt explicitly exempts existing Kanban card status-memory metadata from separate approval.
- [ ] Delegate/subagent runtime-live prompt has the same exception.
- [ ] `kanban_comment` schema tells agents to directly record repo/PR/artifact/progress/verification notes on existing/approved tasks without asking again.
- [ ] The exception explicitly excludes creating new work and dispatch/unblock/complete/archive/delete/status/assignee/priority/public/credential/migration/destructive actions.
- [ ] Prompt/schema tests assert the full forbidden-action list, not only a subset.
- [ ] Gateway intake tests explicitly cover repo URL, PR URL, artifact link, verification summary, and handoff note updates on an existing `t_<hex>` card.
- [ ] Existing-card update conversational intake suppression tests remain green.
- [ ] Focused regression bundle passes.
- [ ] No implementation begins until plan review PASS and user approval.

## Review Gate

Required review: `plan-eng-review` via read-only subagent.

Reviewer must return exactly one of:

```text
PASS
```

or

```text
REQUEST_CHANGES
- blocker: ...
- required fix: ...
```

Review focus:

- Boundary is narrow enough to avoid authorizing dangerous workflow mutations.
- Prompt wording is consistent between main agent and delegate child prompt.
- Tests prove both allowed metadata and forbidden workflow actions remain distinguished.
- No unnecessary new infrastructure/core tool is introduced.
- Plan is executable by a future implementer without guessing.

## Execution Lock

This document is a plan artifact only. Implementation code changes, git commit/push, gateway restart, or live service changes require a separate explicit approval after review PASS.

## Review Evidence

- Status: PASS after focused re-review.
- First `plan-eng-review` result: REQUEST_CHANGES.
  - Blocker 1: forbidden-action assertions covered only a subset.
  - Patch: Tasks 1, 3, and 5 now require the full forbidden-action fragment list across main prompt, delegate prompt, and `KANBAN_COMMENT_SCHEMA`.
  - Blocker 2: gateway intake regression did not explicitly cover repo URL, PR URL, artifact link, verification summary, and handoff note updates.
  - Patch: Task 7 now adds parametrized detector and post-turn fake-detector tests for those five existing-card metadata phrases.
- Focused re-review result: PASS.
  - Reviewer confirmed full forbidden-action boundary assertions across main prompt, delegate prompt, and `KANBAN_COMMENT_SCHEMA`.
  - Reviewer confirmed Task 7 covers repo URL, PR URL, artifact link, verification summary, and handoff note examples on existing `t_<hex>` cards.
  - Reviewer confirmed detector/post-turn tests prevent new-card proposal creation for those existing-card metadata updates.
  - Reviewer confirmed implementation remains locked until user approval.
- Baseline already run before drafting this plan:

```text
venv/bin/python -m pytest tests/gateway/test_kanban_intake_detector.py tests/gateway/test_kanban_intake_post_turn.py -q -o 'addopts='
66 passed, 1 warning in 0.68s
```
