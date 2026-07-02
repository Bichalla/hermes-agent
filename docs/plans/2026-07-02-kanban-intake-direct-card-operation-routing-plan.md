---
schema: pkm-frontmatter/v1
document_id: "kanban-intake-direct-card-operation-routing-plan-20260702"
title: "Kanban Intake Direct Card Operation Routing Plan"
subtitle: "post-turn intake가 이미 실행된/실행 요청된 카드 생성 명령을 default board 후보로 중복 라우팅하지 않게 하는 구현 계획"
created: "2026-07-02T13:05:09+09:00"
updated: "2026-07-02T13:05:09+09:00"
authors:
  - Hermes
owners:
  - honbul
status: draft
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
  Hermes gateway Kanban conversational intake가 사용자의 직접 카드 생성/업데이트 명령을 assistant 응답 이후 다시 default board의 blocked 후보로 제안하는 문제를 고친다. 직접 카드 작업 명령, 후보 기록 요청, 기존 카드 업데이트, live smoke 승인 요청을 명확히 분류하고 detector/post-turn/no-live smoke 회귀 테스트로 고정한다.
tags:
  - hermes-agent
  - kanban
  - gateway
  - conversational-intake
  - writing-plan
  - regression-tdd
aliases:
  - Kanban direct card operation routing hardening
  - Kanban intake 중복 후보 라우팅 수정 계획
projects:
  - Hermes Agent
areas:
  - software-development
  - agent-ops
resources:
  - writing-plans
  - grill-me
  - software-development-lifecycle-operations
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
    - Gateway Kanban intake
    - Hermes Kanban
sources:
  - type: user_request
    title: "Kanban intake 오탐/라우팅 문제 방향성에 맞춘 Writing Plan 요청"
    url: null
    date: 2026-07-02
links:
  canonical: /Users/honbul/.hermes/hermes-agent/docs/plans/2026-07-02-kanban-intake-direct-card-operation-routing-plan.md
  source: null
  related:
    - /Users/honbul/.hermes/hermes-agent/gateway/kanban_intake.py
    - /Users/honbul/.hermes/hermes-agent/gateway/run.py
    - /Users/honbul/.hermes/hermes-agent/tests/gateway/test_kanban_intake_detector.py
    - /Users/honbul/.hermes/hermes-agent/tests/gateway/test_kanban_intake_post_turn.py
    - /Users/honbul/.hermes/hermes-agent/scripts/smoke_kanban_intake_no_live.py
    - /Users/honbul/.hermes/hermes-agent/docs/plans/2026-07-02-kanban-status-memory-hardening-plan.md
relations:
  parent: null
  children: []
  depends_on:
    - /Users/honbul/.hermes/docs/First Landing Package.md
    - /Users/honbul/.hermes/skills/software-development/writing-plans/SKILL.md
    - /Users/honbul/.hermes/skills/software-development/software-development-lifecycle-operations/references/gateway-post-turn-detector-regression-tdd.md
    - /Users/honbul/.hermes/skills/software-development/software-development-lifecycle-operations/references/gateway-kanban-current-turn-command-suppression.md
    - /Users/honbul/.hermes/skills/software-development/software-development-lifecycle-operations/references/gateway-kanban-existing-card-update-suppression.md
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

# Kanban Intake Direct Card Operation Routing Plan

> **For Hermes:** Use `subagent-driven-development` or equivalent task-by-task TDD only after this plan receives review PASS and the user explicitly approves implementation. Current working-tree changes are WIP evidence, not an approved final implementation.

**Goal:** Gateway post-turn Kanban intake must not create a second blocked proposal on the configured default board when the latest user message was a direct Kanban card operation request already handled, attempted, or blocked by the main turn.

**Architecture:** Replace the current broad `explicit_card_request` heuristic with a small deterministic user-intent classifier. The classifier separates direct card operations from proposal-record requests, read-only candidate audits, existing-card updates, and approved live-smoke requests. Detector-level, post-turn fake-detector, and no-live smoke tests must all enforce the same contract.

**Tech Stack:** Python, pytest, Hermes Gateway, `gateway/kanban_intake.py`, Kanban SQLite board helpers, no-live smoke script.

---

## Policy Block

```yaml
risk_class: S2
policy_source: /Users/honbul/.hermes/docs/hermes-autonomous-recursive-improvement-policy-20260429.md
required_reviews:
  - plan-eng-review
execution_allowed: false
execution_scope: plan_only_until_review_pass_and_user_approval
measurement_source: focused_gateway_pytest_bundle_and_no_live_smoke
public_surface_policy: none
forbidden_without_confirmation:
  - implementation_code_change
  - git_commit
  - git_push
  - gateway_restart
  - live_kanban_db_mutation
  - discord_live_send
  - public_deploy
  - cron_mutation
  - credential_read
  - destructive_delete
```

## Grill Summary

- **Confirmed goal:** Kanban intake가 “카드 만들어줘” 같은 직접 카드 작업 명령을 assistant 응답 뒤에 다시 default board 후보로 제안하지 않도록 routing/detector 알고리즘을 고친다.
- **Target user / actor:** Discord/CLI에서 Hermes Kanban을 쓰는 honbul, gateway post-turn intake, Hermes main agent, Kanban worker/subagent.
- **Current state checked:**
  - `gateway/run.py:7930-8004`: assistant response 이후 `_maybe_build_kanban_intake_proposal_message()`가 detector를 실행하고, detector가 true면 `card_proposal_eligibility()`를 다시 확인한 뒤 pending proposal을 저장한다.
  - `gateway/kanban_intake.py:53-56`: `_EXPLICIT_CARD_REQUEST_RE`가 “카드 만들어/생성/카드로 남겨/칸반에 추가”를 한 묶음으로 본다.
  - `gateway/kanban_intake.py:863-882`: 현재 eligibility는 approved live smoke, existing-card update, read-only audit, explicit card request, vague board, durable follow-up 순으로 판단한다.
  - `build_detection_request()`는 user/assistant text를 `minimize_for_detector(..., max_chars=500)`로 축약한다. assistant-side `t_<hex>` 근거만으로는 long response에서 miss 가능하다.
  - 현재 WIP diff에는 fulfilled-card suppress, existing-card metadata suppress, status-memory approval-boundary prompt/schema 변경이 섞여 있다.
- **Constraints:** user-only current-turn intent가 기준이어야 한다. Assistant summary가 새 카드 승인이나 durable follow-up을 만들어내면 안 된다. Live gateway restart, live DB mutation, commit/push는 별도 승인 전까지 금지한다. Prompt caching을 깨는 동적 prompt mutation은 하지 않는다.
- **Non-goals:** 이번 plan은 gateway restart, live smoke, pending intake DB cleanup, status-memory approval-boundary prompt 변경, Kanban worker dispatch policy 변경을 포함하지 않는다.
- **Success criteria:** reported class가 detector/post-turn/no-live smoke에서 억제된다. Explicit proposal-record requests와 approved live smoke positives는 유지된다. Failure/negation 문장이 `fulfilled`로 오분류되지 않는다. `lifelog-control` default board fallback이 direct board command에서 발생하지 않는다.
- **Failure criteria:** `suttanipata-ko 보드에 카드 만들어줘` 같은 직접 명령이 `lifelog-control` 후보로 뜨거나, `카드 생성 실패`가 fulfilled로 기록되거나, 반대로 `이 작업은 카드로 남겨줘` 같은 proposal-record request가 죽는 경우.
- **Domain terms / Ubiquitous Language candidates:** 아래 DDD Light 참조.
- **Open questions:** 없음. Board-name parsing은 이번 phase에서 “proposal routing 개선”이 아니라 “direct operation suppress”로 해결한다.
- **Assumptions accepted:** 사용자는 코드 실행이 아니라 Writing Plan 작성을 승인했다. 현재 WIP implementation은 유지하되 final implementation으로 간주하지 않는다.

## DDD Light

### Bounded Context

- **Context name:** Gateway Kanban conversational intake routing
- **Boundary:** `gateway/kanban_intake.py` deterministic intent gates, `gateway/run.py` post-turn re-check, gateway intake tests, no-live smoke.
- **Out of boundary:** live gateway restart, actual Discord send, live Kanban DB cleanup, status-memory prompt/tool policy changes, board management UX.

### Ubiquitous Language

| Term | Meaning | Do Not Confuse With | Example |
|---|---|---|---|
| Direct card operation | User asks the agent to create/update a concrete Kanban card as the current turn action | Proposal-record request | `suttanipata-ko 보드에 카드 만들어줘` |
| Proposal-record request | User asks to leave a new tracking/proposal card candidate for later review | Direct tool execution | `이 작업은 카드로 남겨줘: Kanban intake 회귀 테스트 추가` |
| Existing-card update | User asks to record/update/comment on an already identified card | New card proposal | `t_deadbeef 카드에 PR URL 기록해줘` |
| Post-turn intake | Gateway guard that may propose a blocked card after the assistant response | Main agent tool execution | `_maybe_build_kanban_intake_proposal_message()` |
| Default board fallback | Intake uses configured `default_board` when proposal has no board | User-requested board routing | `lifelog-control` candidate for `suttanipata-ko` request |
| Fulfilled card creation | Assistant reports a successful created card id for the current request | Failed/blocked attempt mentioning an id | `카드 만들었어: t_e9f4c088` |

### Core Scenarios

- **Scenario 1:** User says `suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘`; assistant reports `t_e9f4c088`. Post-turn intake must return `None`.
- **Scenario 2:** User says the same direct create request; assistant says creation failed or approval is needed. Post-turn intake still must not create a fallback `lifelog-control` proposal; the main response owns the failure/blocker.
- **Scenario 3:** User says `이 작업은 카드로 남겨줘: Kanban intake 중복 노이즈 회귀 테스트 추가`. This remains eligible as a proposal-record request.
- **Scenario 4:** User says `승인: Discord live smoke 테스트 blocked card 1개 생성/검증해`. This remains eligible under the explicit approved live smoke rule.
- **Scenario 5:** User says `t_deadbeef 카드에 PR URL 기록해줘`. This remains existing-card update suppression, not new-card proposal.

### Invariants

- User text owns intent classification. Assistant text may confirm fulfillment/attempt outcome but must not create user approval.
- Direct card operation requests never become default-board post-turn proposals.
- Proposal-record requests may become blocked proposals only when user explicitly asks to record/leave a proposal/tracking card.
- Approved live smoke remains a narrow positive override before suppression.
- Existing-card update/comment/progress/link operations are not new-card creation intents.
- Failure/negation wording must not be labeled `fulfilled_current_turn_card_creation`.

---

## Verified Context Inventory

| Source | Checked fact | Plan implication |
|---|---|---|
| `gateway/run.py:7930-8004` | Post-turn proposal path re-checks `card_proposal_eligibility()` after detector output and before store write. | Fix must live in deterministic eligibility, not only detector prompt/title generation. |
| `gateway/kanban_intake.py:53-56` | Direct create and proposal-record phrases are collapsed into `_EXPLICIT_CARD_REQUEST_RE`. | Need a narrower classifier instead of one broad regex. |
| `gateway/kanban_intake.py:806-818` | Existing-card update suppression already uses user text only. | Reuse the user-text-only pattern for direct card operations. |
| `gateway/kanban_intake.py:821-836` current WIP | Fulfilled detection uses assistant text and can misclassify failure sentences. | Replace or rename with a safer attempted/handled/direct-operation rule and negation tests. |
| `tests/gateway/test_kanban_intake_detector.py` | Detector tests already cover meta, read-only audit, current-turn review command, existing-card update, and positives. | Add new RED tests next to existing suppression/positive matrix. |
| `tests/gateway/test_kanban_intake_post_turn.py` | Fake detector tests prove post-turn re-check cannot be bypassed. | Add direct-card-operation fake-detector suppressions. |
| `scripts/smoke_kanban_intake_no_live.py` | No-live smoke already asserts no gateway restart, Discord send, live board creation, cron/Lifelog/Graphify/JÖKL public mutation. | Add direct-card-operation suppressed flag without live side effects. |
| `docs/plans/2026-07-02-kanban-status-memory-hardening-plan.md` | Separate status-memory approval-boundary work exists. | Do not mix that policy diff into this plan’s implementation commit. |

---

## Implementation Plan

### Task 0: Freeze and separate current WIP scope

**Objective:** Prevent the accidental current implementation from being committed as a mixed patch.

**Files:**
- Inspect only: current working tree diff
- No production file modification in this task

**Step 1: Capture scoped status**

Run:

```bash
git status --short --branch
git diff --stat
git diff -- gateway/kanban_intake.py tests/gateway/test_kanban_intake_detector.py tests/gateway/test_kanban_intake_post_turn.py tests/gateway/test_kanban_intake_no_live_smoke.py scripts/smoke_kanban_intake_no_live.py
```

Expected:

- Working tree may contain unrelated WIP for status-memory approval-boundary files.
- This plan’s implementation must stage only the intake-routing files listed above.

**Step 2: Decide scope boundary**

Document in implementation notes:

- `agent/prompt_builder.py`, `tools/delegate_tool.py`, `tools/kanban_tools.py`, `tests/agent/test_system_prompt.py`, `tests/tools/test_delegate.py`, `tests/tools/test_kanban_tools.py` belong to the separate status-memory plan.
- Do not stage or commit those files for this intake-routing fix.

**Verification:**

```bash
git diff --name-only
```

Expected: implementer can clearly separate intake-routing files from status-memory files.

---

### Task 1: Add RED detector tests for direct card operation suppression

**Objective:** Define direct card operations as a suppression class distinct from proposal-record requests.

**Files:**
- Modify: `tests/gateway/test_kanban_intake_detector.py`
- Later modify: `gateway/kanban_intake.py`

**Step 1: Add failing tests**

Add tests like:

```python
@pytest.mark.parametrize(("user_summary", "assistant_summary"), [
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "실제로 필요한 카드는 이미 `suttanipata-ko` 보드에 만들었어: `t_e9f4c088`.",
    ),
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "카드 생성 실패했어. 권한 문제를 먼저 해결해야 해.",
    ),
    (
        "create a card on suttanipata-ko for translation review",
        "I could not create the card because the board was not found.",
    ),
])
def test_direct_card_operation_request_is_not_post_turn_proposal(user_summary, assistant_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary=assistant_summary,
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "direct_card_operation_intent"
    assert KeywordHeuristicDetector().detect(request).card_worthy is False
```

Add a separate regression for failure wording not being labeled fulfilled:

```python
def test_failed_card_creation_is_not_labeled_fulfilled():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="suttanipata-ko 보드에 카드 만들어줘",
        assistant_summary="카드 생성 실패했어. 이전 후보 `t_e9f4c088`가 남아있어.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "direct_card_operation_intent"
```

**Step 2: Verify RED**

Run:

```bash
venv/bin/python -m pytest tests/gateway/test_kanban_intake_detector.py -q -o 'addopts='
```

Expected before implementation: new tests fail because current code returns `explicit_card_request` or `fulfilled_current_turn_card_creation`.

---

### Task 2: Preserve explicit proposal-record positives

**Objective:** Ensure suppression does not kill legitimate “leave this as a card/proposal” requests.

**Files:**
- Modify: `tests/gateway/test_kanban_intake_detector.py`

**Step 1: Add positive tests**

Add or extend positive cases:

```python
@pytest.mark.parametrize("user_summary", [
    "이 작업은 카드로 남겨줘: Kanban intake 중복 노이즈 회귀 테스트 추가",
    "새 tracking card 후보로 올려: Kanban intake 중복 노이즈 회귀 테스트 추가",
    "별도 카드 후보로 남겨: direct card operation suppression 설계 리뷰",
])
def test_proposal_record_requests_remain_eligible(user_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="후속 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is True
    assert eligibility.matched_rule == "explicit_card_request"
    assert KeywordHeuristicDetector().detect(request).card_worthy is True
```

**Step 2: Verify RED/GREEN relationship**

Run the detector file. Expected after Task 1 RED and before implementation: direct-operation negatives fail, positives should keep passing or be adjusted to pass after implementation.

---

### Task 3: Implement deterministic user-intent classification

**Objective:** Replace the broad `explicit_card_request` branch with narrower user-intent classification.

**Files:**
- Modify: `gateway/kanban_intake.py`

**Step 1: Add regex helpers**

Implement helpers near the existing regex section:

```python
_DIRECT_CARD_OPERATION_RE = re.compile(
    r"(?:"
    r"(?:[\w가-힣_.-]+\s*)?보드(?:에|로)?\s*.*카드\s*(?:만들|생성|추가)"
    r"|create\s+(?:a\s+)?card\s+(?:on|in)\s+[\w_.-]+"
    r"|add\s+(?:a\s+)?card\s+(?:to|on|in)\s+[\w_.-]+"
    r")",
    re.I,
)
_PROPOSAL_RECORD_REQUEST_RE = re.compile(
    r"(?:카드로\s*남겨|카드\s*후보(?:로)?\s*(?:올려|남겨)|"
    r"새\s*(?:tracking\s*)?카드\s*후보|새\s*tracking\s*card\s*후보|"
    r"proposal\s+card|tracking\s+card\s+candidate)",
    re.I,
)
```

Keep `_EXPLICIT_CARD_REQUEST_RE` only if needed as an umbrella, but do not let it alone decide eligibility.

**Step 2: Add classifier**

```python
def _has_direct_card_operation_request(text: str) -> bool:
    return bool(_DIRECT_CARD_OPERATION_RE.search(text or ""))


def _has_proposal_record_request(text: str) -> bool:
    return bool(_PROPOSAL_RECORD_REQUEST_RE.search(text or ""))
```

If keeping fulfillment detection, rename it so it does not imply success when the actual suppression reason is direct operation:

```python
def suppress_direct_card_operation_intent(request: IntakeDetectionRequest) -> bool:
    user_text = " ".join(str(request.user_summary or "").split())
    if not user_text:
        return False
    if _has_proposal_record_request(user_text):
        return False
    return _has_direct_card_operation_request(user_text)
```

**Step 3: Update eligibility order**

Recommended ordering:

```python
if _is_meta_or_one_off(text):
    return ProposalEligibility(False, "meta discussion or one-off question", "negative_meta_one_off")
if _is_approved_live_smoke_request(user_text):
    return ProposalEligibility(True, "approved live smoke request", "approved_live_smoke_request")
if suppress_existing_card_update_intent(user_text) and not _has_proposal_record_request(user_text):
    return ProposalEligibility(False, "existing card update intent", "existing_card_update_intent")
if _is_read_only_candidate_audit(user_text) and not _has_proposal_record_request(user_text):
    return ProposalEligibility(False, "read-only candidate audit", "read_only_candidate_audit")
if suppress_direct_card_operation_intent(request):
    return ProposalEligibility(False, "direct card operation intent", "direct_card_operation_intent")
if _has_proposal_record_request(user_text):
    return ProposalEligibility(True, "explicit card request", "explicit_card_request")
```

Only after those branches should vague board and durable follow-up rules run.

**Step 4: Remove or narrow misleading fulfilled helper**

Either:

- Remove `_CARD_CREATION_FULFILLED_RE` and `suppress_fulfilled_current_turn_card_creation()` entirely, because direct-operation suppression covers success/failure/attempted states; or
- Keep a separate helper only for telemetry/title purposes, with explicit failure negation guards.

Preferred: remove it from eligibility to avoid assistant-summary dependence and 500-char truncation brittleness.

**Step 5: Verify GREEN**

Run:

```bash
venv/bin/python -m pytest tests/gateway/test_kanban_intake_detector.py -q -o 'addopts='
```

Expected: detector tests pass.

---

### Task 4: Add post-turn fake-detector bypass tests

**Objective:** Prove an auxiliary/fake detector cannot bypass deterministic suppression.

**Files:**
- Modify: `tests/gateway/test_kanban_intake_post_turn.py`

**Step 1: Add suppress tests**

Add tests like:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "assistant_response"), [
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "실제로 필요한 카드는 이미 `suttanipata-ko` 보드에 만들었어: `t_e9f4c088`.",
    ),
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "카드 생성 실패했어. 권한 문제를 먼저 해결해야 해.",
    ),
])
async def test_post_turn_suppresses_direct_card_operation_even_if_detector_says_true(tmp_path, text, assistant_response):
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda _cfg=None: __import__("gateway.kanban_intake", fromlist=["PendingKanbanStore"]).PendingKanbanStore(cfg.store_path)
    runner._kanban_intake_detector = Detector(DetectorDecision(
        True,
        title="Improve Kanban intake title generator",
        body={"source_ref": "kp_safe"},
    ))
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source(), message_id="1521543610456084607")

    msg = await runner._maybe_build_kanban_intake_proposal_message(event, "s1", event.text, assistant_response)
    assert msg is None
```

**Step 2: Add positive post-turn proposal-record test if not already covered**

Ensure existing `test_post_turn_renders_semantic_title_for_explicit_candidate_audit_card_request` or a new positive still renders when the user asks to record a proposal card.

**Step 3: Verify**

```bash
venv/bin/python -m pytest tests/gateway/test_kanban_intake_post_turn.py -q -o 'addopts='
```

Expected: all post-turn tests pass.

---

### Task 5: Update no-live smoke flags

**Objective:** Make the no-live script prove this invariant without gateway restart or live sends.

**Files:**
- Modify: `scripts/smoke_kanban_intake_no_live.py`
- Modify: `tests/gateway/test_kanban_intake_no_live_smoke.py`

**Step 1: Add smoke booleans**

In `scripts/smoke_kanban_intake_no_live.py`, add:

```python
direct_card_operation_suppressed = not detector.detect(IntakeDetectionRequest(
    platform="discord",
    session_key="s1",
    source_ref="kp_safe",
    user_summary="suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
    assistant_summary="실제로 필요한 카드는 이미 `suttanipata-ko` 보드에 만들었어: `t_e9f4c088`.",
    default_board=board,
    default_tenant="lifelog",
)).card_worthy

direct_card_operation_failure_suppressed = not detector.detect(IntakeDetectionRequest(
    platform="discord",
    session_key="s1",
    source_ref="kp_safe",
    user_summary="suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
    assistant_summary="카드 생성 실패했어. 권한 문제를 먼저 해결해야 해.",
    default_board=board,
    default_tenant="lifelog",
)).card_worthy
```

Add both to JSON and PASS criteria.

**Step 2: Assert in test**

In `tests/gateway/test_kanban_intake_no_live_smoke.py`:

```python
assert data["direct_card_operation_suppressed"] is True
assert data["direct_card_operation_failure_suppressed"] is True
```

**Step 3: Verify**

```bash
venv/bin/python scripts/smoke_kanban_intake_no_live.py --json
venv/bin/python -m pytest tests/gateway/test_kanban_intake_no_live_smoke.py -q -o 'addopts='
```

Expected JSON includes both flags as `true`, while all live side-effect booleans remain `false`.

---

### Task 6: Remove mixed status-memory changes from this implementation scope

**Objective:** Keep this fix reviewable and avoid mixing approval-boundary prompt policy with intake routing.

**Files:**
- Do not stage for this plan:
  - `agent/prompt_builder.py`
  - `tools/delegate_tool.py`
  - `tools/kanban_tools.py`
  - `tests/agent/test_system_prompt.py`
  - `tests/tools/test_delegate.py`
  - `tests/tools/test_kanban_tools.py`

**Step 1: Confirm unrelated WIP remains separate**

Run:

```bash
git diff --name-only
```

Expected: status-memory files may remain dirty, but they are excluded from this plan’s staging and validation claim.

**Step 2: If implementation needs a clean branch**

Do not rollback without user approval. Instead either:

- leave unrelated WIP unstaged; or
- create a patch backup file outside the commit path after user approval; or
- commit status-memory work separately only after its own review/approval.

**Verification:** final `git diff --cached --name-only` for this plan must contain only intake-routing files and this plan doc if committing is later approved.

---

### Task 7: Run focused verification bundle

**Objective:** Prove detector, post-turn, smoke, syntax, and whitespace invariants.

**Files:**
- Read/execute only unless fixing failures.

**Commands:**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  tests/gateway/test_kanban_intake_no_live_smoke.py \
  -q -o 'addopts='
venv/bin/python scripts/smoke_kanban_intake_no_live.py --json
venv/bin/python -m py_compile \
  gateway/kanban_intake.py \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  scripts/smoke_kanban_intake_no_live.py
git diff --check -- \
  gateway/kanban_intake.py \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  tests/gateway/test_kanban_intake_no_live_smoke.py \
  scripts/smoke_kanban_intake_no_live.py \
  docs/plans/2026-07-02-kanban-intake-direct-card-operation-routing-plan.md
```

**Expected:**

- Gateway intake focused tests pass.
- No-live smoke shows no gateway restart, Discord live send, live board creation, cron mutation, Lifelog DB mutation, Graphify run, or JÖKL public mutation.
- `direct_card_operation_suppressed` and `direct_card_operation_failure_suppressed` are `true`.
- `approved_live_smoke` and proposal-record positives remain eligible.
- `py_compile` and `git diff --check` pass.

---

### Task 8: Update skill/reference only if implementation reveals a durable procedure correction

**Objective:** Keep procedural memory aligned without making the plan commit too broad.

**Files:**
- Optional modify after verified implementation:
  - `/Users/honbul/.hermes/skills/software-development/software-development-lifecycle-operations/references/gateway-post-turn-detector-regression-tdd.md`

**Step 1: Patch only if needed**

If the final implementation changes the durable lesson, patch the reference with the exact contract:

- Direct card operation requests are suppressed in post-turn intake.
- Proposal-record requests remain eligible.
- Existing-card update suppression remains separate.
- Approved live smoke remains a narrow positive override.

**Step 2: Keep skill update separate if repo boundaries require it**

This skill lives under `~/.hermes/skills`, not the Hermes Agent repo. Do not claim it as part of the Hermes Agent commit unless intentionally staging the Hermes home repo separately.

---

## Acceptance Criteria

- [ ] `card_proposal_eligibility()` returns `direct_card_operation_intent` for direct card operation requests, regardless of assistant success/failure wording.
- [ ] `card_proposal_eligibility()` keeps proposal-record requests eligible as `explicit_card_request`.
- [ ] Approved live smoke remains eligible as `approved_live_smoke_request`.
- [ ] Existing-card update/comment/progress/link requests remain suppressed as `existing_card_update_intent`.
- [ ] Post-turn fake-detector tests prove deterministic suppression cannot be bypassed by `DetectorDecision(card_worthy=True)`.
- [ ] No-live smoke includes direct-operation success and failure suppression flags and all side-effect booleans remain false.
- [ ] Status-memory approval-boundary prompt/schema changes are not mixed into this intake-routing commit.
- [ ] Gateway is not restarted and no live pending DB cleanup occurs without explicit current-turn approval.

## Review Gate

Required before implementation:

```yaml
plan-eng-review:
  status: pending
  scope:
    - intent taxonomy is complete enough for reported bug class
    - test matrix preserves positive paths
    - implementation avoids assistant-summary approval forgery
    - no-live smoke covers side-effect boundary
    - diff scope excludes status-memory approval-boundary changes
```

Implementation remains locked until review PASS and explicit user approval.

## Commit / Staging Policy

No commit/push is approved by this plan. If later approved, stage narrowly:

```bash
git add \
  gateway/kanban_intake.py \
  tests/gateway/test_kanban_intake_detector.py \
  tests/gateway/test_kanban_intake_post_turn.py \
  tests/gateway/test_kanban_intake_no_live_smoke.py \
  scripts/smoke_kanban_intake_no_live.py \
  docs/plans/2026-07-02-kanban-intake-direct-card-operation-routing-plan.md
```

Do not stage status-memory files listed in Task 6 for this commit.

## Rollback Plan

If implementation causes false negatives for legitimate proposal-record requests:

1. Revert only the intent-classifier and related tests for this plan’s scoped files.
2. Keep existing safe suppressions for read-only audit, current-turn subagent review command, and existing-card update if unaffected.
3. Re-run the focused test bundle.
4. Do not restart gateway until a passing fix is approved.

## Final Verification Checklist

- [ ] Plan file has PKM frontmatter.
- [ ] `execution_allowed: false` is present.
- [ ] Required review is pending and not claimed PASS.
- [ ] Verified context inventory cites concrete files/lines.
- [ ] Tasks are TDD-oriented and file-scoped.
- [ ] No live side effect is requested by the plan itself.
