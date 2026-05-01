---
schema: pkm-frontmatter/v1
document_id: "provider-auto-guard-implementation-plan-20260426"
title: "Hermes Provider Auto Guard Implementation Plan"
subtitle: "gpt-5.5와 provider=auto 조합의 Z.AI/GLM 오라우팅 방어"
created: "2026-04-26T15:33:33+09:00"
updated: "2026-04-26T15:33:33+09:00"
authors:
  - Hermes
owners:
  - honbul
status: draft
lifecycle: seed
document_type: plan
audience:
  - honbul
  - Hermes
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: high
summary: >-
  Hermes에서 provider=auto와 model=gpt-5.5가 함께 쓰일 때 GLM/Z.AI 등으로 잘못 라우팅되는 문제를 방어하기 위한 구현 계획과 새 세션용 시작 프롬프트.
tags:
  - hermes
  - provider-routing
  - openai-codex
  - z-ai
  - glm
  - xiaomi
  - guardrail
aliases:
  - provider auto guard plan
  - gpt-5.5 auto routing guard
projects:
  - hermes-agent
areas:
  - ai-agent-ops
  - developer-tools
resources: []
entities:
  people:
    - honbul
  organizations: []
  brands: []
  products:
    - Hermes Agent
    - OpenAI Codex
    - Z.AI GLM
    - Xiaomi MiMo
  systems:
    - /Users/honbul/.hermes/hermes-agent
sources:
  - type: user_request
    title: "provider auto 문제 방어 구현 요청"
    url: null
    date: "2026-04-26"
links:
  canonical: /Users/honbul/.hermes/hermes-agent/docs/plans/provider-auto-guard-implementation-plan.md
  source: null
  related:
    - /Users/honbul/.hermes/config.yaml
    - /Users/honbul/.hermes/hermes-agent/hermes_cli/runtime_provider.py
    - /Users/honbul/.hermes/hermes-agent/hermes_cli/models.py
    - /Users/honbul/.hermes/hermes-agent/hermes_cli/codex_models.py
relations:
  parent: null
  children: []
  depends_on: []
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
automation:
  indexing: true
  extract_tasks: true
  sync_targets:
    - local_markdown
version: 0.1.0
---

# Hermes Provider Auto Guard Implementation Plan

> For Hermes: 새 세션에서는 `writing-plans`, `test-driven-development`, `systematic-debugging`, 필요 시 `codex`/`hermes-agent` skill을 로드하고 이 계획을 그대로 실행한다.

Goal: `provider=auto`와 `model=gpt-5.5` 조합이 Z.AI/GLM/Xiaomi MiMo 같은 비-Codex provider로 잘못 라우팅되는 것을 코드 레벨에서 방어한다.

Architecture: 두 겹의 방어를 둔다. 1차는 model-to-provider detection에서 Codex 전용 모델명(`gpt-5.5` 등)을 `openai-codex`로 명확히 매핑한다. 2차는 최종 runtime provider 확정 직전에 provider/model compatibility guard를 실행해, provider가 선택됐더라도 해당 provider가 모델을 지원하지 않으면 API 호출 전에 명확한 에러를 내거나 Codex로 안전하게 재선택한다.

Tech Stack: Python, pytest, Hermes CLI runtime provider layer, OpenAI Codex transport.

---

## Problem Statement

현재 확인된 위험 조건:

- `/Users/honbul/.hermes/config.yaml`의 현재 정상값은 `model.provider: openai-codex`, `model.default: gpt-5.5`이다.
- 하지만 CLI override 또는 old session에서 `--provider auto -m gpt-5.5`가 실행되면, `.env`의 `GLM_API_KEY` 때문에 `auto` resolver가 Z.AI를 선택할 수 있다.
- 그 결과 `gpt-5.5`가 `https://api.z.ai/api/coding/paas/v4/chat/completions`로 전송되고 `Unknown Model` 400이 발생한다.
- 이는 OAuth 문제가 아니라 provider/model mismatch 문제다.

Non-goals:

- GLM/Z.AI 또는 Xiaomi MiMo credential을 제거하지 않는다.
- `provider=auto` 자체를 폐기하지 않는다.
- Codex OAuth token, API key, refresh token 등 credential 원문을 출력하지 않는다.

Acceptance criteria:

1. `hermes --provider auto -m gpt-5.5 -z 'Respond exactly: AUTO_CODEX_OK'`가 Z.AI가 아니라 OpenAI Codex endpoint로 간다. 또는 최소한 Z.AI로 보내기 전에 명확한 provider/model mismatch 에러를 낸다.
2. `hermes --provider openai-codex -m gpt-5.5 -z ...`는 계속 정상 동작한다.
3. `hermes --provider zai -m glm-5.1 -z ...`는 기존대로 정상 동작한다.
4. `hermes --provider xiaomi -m mimo-v2.5-pro -z ...`는 기존대로 정상 동작한다.
5. focused pytest가 통과한다.
6. request dump 또는 debug log에서 `provider=auto + model=gpt-5.5`가 Z.AI endpoint로 나가지 않았음을 확인한다.

---

## Files to inspect first

- `/Users/honbul/.hermes/hermes-agent/hermes_cli/runtime_provider.py`
- `/Users/honbul/.hermes/hermes-agent/hermes_cli/models.py`
- `/Users/honbul/.hermes/hermes-agent/hermes_cli/codex_models.py`
- `/Users/honbul/.hermes/hermes-agent/hermes_cli/oneshot.py`
- `/Users/honbul/.hermes/hermes-agent/cli.py`
- Existing tests under `/Users/honbul/.hermes/hermes-agent/tests/` that mention provider detection, runtime provider, Codex, Z.AI, Xiaomi, or model registry.

Useful searches:

```bash
cd /Users/honbul/.hermes/hermes-agent
python - <<'PY'
from pathlib import Path
for pat in ['resolve_runtime_provider', 'detect_provider_for_model', 'openai-codex', 'gpt-5.5', 'GLM_API_KEY', 'XIAOMI_API_KEY']:
    print('\n###', pat)
    for p in Path('.').rglob('*.py'):
        if any(part in {'.git','venv','__pycache__'} for part in p.parts):
            continue
        try:
            s = p.read_text(errors='ignore')
        except Exception:
            continue
        if pat in s:
            print(p)
PY
```

---

## Recommended behavior

Preferred behavior:

- If provider is explicit and model belongs to another known provider, fail fast with a clear error.
  - Example: `--provider zai -m gpt-5.5` should not silently call Z.AI.
- If provider is `auto`, model name should strongly influence provider selection before API-key availability.
  - Example: `--provider auto -m gpt-5.5` should resolve to `openai-codex` because `gpt-5.5` is known Codex-only in this Hermes setup.
- If model is unknown and provider is `auto`, existing env/key-based fallback can remain.

Clear error wording example:

```text
Provider/model mismatch: provider 'zai' does not support model 'gpt-5.5'. Detected provider for this model is 'openai-codex'. Use --provider openai-codex -m gpt-5.5, or choose a Z.AI model such as glm-5.1.
```

---

## Task 1: Add failing tests for auto + Codex model detection

Objective: Capture the exact regression: `provider=auto`, `model=gpt-5.5`, GLM/Xiaomi keys present, should not resolve to Z.AI/Xiaomi.

Files:

- Modify or create test near runtime provider tests, likely:
  - `/Users/honbul/.hermes/hermes-agent/tests/hermes_cli/test_runtime_provider.py`
  - or existing equivalent discovered by search.

Test cases to add:

```python
def test_auto_provider_prefers_codex_for_gpt_55_even_when_glm_key_present(monkeypatch):
    monkeypatch.setenv('GLM_API_KEY', 'redacted-test-key')
    monkeypatch.setenv('XIAOMI_API_KEY', 'redacted-test-key')

    # Use the actual project helper once discovered. The assertion is the key contract:
    provider = detect_provider_for_model('gpt-5.5')
    assert provider == 'openai-codex'
```

If the actual helper is `resolve_runtime_provider(...)`, shape the test around that API instead:

```python
def test_auto_provider_with_gpt_55_resolves_to_openai_codex(monkeypatch):
    monkeypatch.setenv('GLM_API_KEY', 'redacted-test-key')
    monkeypatch.setenv('XIAOMI_API_KEY', 'redacted-test-key')

    resolved = resolve_runtime_provider(
        provider='auto',
        model='gpt-5.5',
        # pass required config/auth args according to actual signature
    )

    assert resolved.provider == 'openai-codex'
```

Run expected failing test:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest tests/hermes_cli/test_runtime_provider.py -q
```

Expected before implementation: FAIL, showing Z.AI/Xiaomi/auto fallback wins over Codex model detection or `gpt-5.5` is unknown to detection.

---

## Task 2: Ensure Codex model registry exposes `gpt-5.5`

Objective: Make model detection know that `gpt-5.5` belongs to `openai-codex`.

Files:

- Inspect/modify: `/Users/honbul/.hermes/hermes-agent/hermes_cli/codex_models.py`
- Inspect/modify: `/Users/honbul/.hermes/hermes-agent/hermes_cli/models.py`

Implementation direction:

- Find the central list/dict of Codex models.
- Confirm these aliases are present if supported by current Hermes:
  - `gpt-5.5`
  - any existing Codex aliases used in config/status.
- Ensure the public model detection function sees this registry.

Pseudo-code shape:

```python
CODEX_MODELS = {
    'gpt-5.5': {...},
    # existing codex models
}
```

or if the code uses a list:

```python
OPENAI_CODEX_MODELS = {
    'gpt-5.5',
    # existing models
}
```

Verification:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python - <<'PY'
from hermes_cli.models import detect_provider_for_model
print(detect_provider_for_model('gpt-5.5'))
PY
```

Expected:

```text
openai-codex
```

---

## Task 3: Move model-based provider detection ahead of env-key fallback for `auto`

Objective: When provider is `auto`, known model names must beat available API keys.

Files:

- Modify: `/Users/honbul/.hermes/hermes-agent/hermes_cli/runtime_provider.py`

Implementation direction:

Current likely problematic flow:

1. provider is `auto`
2. active OAuth / env key fallback chooses Z.AI because `GLM_API_KEY` exists
3. model `gpt-5.5` is sent to Z.AI

Desired flow:

1. provider is `auto`
2. if model is known to belong to a provider, choose that provider first
3. only if model is unknown or generic, fall back to active OAuth/env key logic

Pseudo-code:

```python
if provider in (None, 'auto') and model:
    detected_provider = detect_provider_for_model(model)
    if detected_provider:
        provider = detected_provider
```

Important details:

- Do not break provider-specific aliases.
- Do not force Codex for generic models unless the registry says so.
- Keep explicit provider behavior separate from auto behavior.

Run:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest tests/hermes_cli/test_runtime_provider.py -q
```

Expected: new auto Codex test passes.

---

## Task 4: Add provider/model compatibility guard for explicit mismatches

Objective: Prevent calls like `--provider zai -m gpt-5.5` from reaching the wrong endpoint.

Files:

- Modify: `/Users/honbul/.hermes/hermes-agent/hermes_cli/runtime_provider.py`
- Add tests in runtime provider test file.

Behavior:

- If provider is explicit, model is known, and detected provider conflicts with explicit provider, raise a clear error before network call.
- Exception: if there are intentional cross-provider compatible aliases in Hermes, preserve them via allowlist.

Pseudo-code:

```python
def validate_provider_model_compatibility(provider: str, model: str) -> None:
    if not provider or provider == 'auto' or not model:
        return
    detected = detect_provider_for_model(model)
    if detected and normalize_provider(detected) != normalize_provider(provider):
        raise ProviderModelMismatchError(
            f"Provider/model mismatch: provider '{provider}' does not support model '{model}'. "
            f"Detected provider for this model is '{detected}'."
        )
```

Test cases:

```python
def test_explicit_zai_with_gpt_55_fails_fast():
    with pytest.raises(Exception, match='Provider/model mismatch'):
        validate_provider_model_compatibility('zai', 'gpt-5.5')


def test_explicit_openai_codex_with_gpt_55_allowed():
    validate_provider_model_compatibility('openai-codex', 'gpt-5.5')
```

Run:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest tests/hermes_cli/test_runtime_provider.py -q
```

Expected: pass.

---

## Task 5: Add integration-level CLI smoke test or dry-run-safe test

Objective: Verify the CLI path uses the new guard/detection, not only unit helpers.

Files:

- Existing CLI tests under `/Users/honbul/.hermes/hermes-agent/tests/hermes_cli/`

Preferred no-network test:

- Patch/mock the transport creation layer and assert provider `openai-codex` is selected for `provider=auto`, `model=gpt-5.5`.
- Avoid hitting real ChatGPT/Z.AI in pytest.

Pseudo-test:

```python
def test_cli_auto_gpt_55_selects_openai_codex(monkeypatch):
    monkeypatch.setenv('GLM_API_KEY', 'redacted-test-key')
    monkeypatch.setenv('XIAOMI_API_KEY', 'redacted-test-key')
    # invoke CLI config/provider resolution function directly or with CliRunner
    # assert selected provider == 'openai-codex'
```

Run focused tests:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest \
  tests/hermes_cli/test_runtime_provider.py \
  tests/hermes_cli/test_web_server.py \
  tests/agent/transports/test_codex_transport.py \
  -q
```

Expected: pass.

---

## Task 6: Manual smoke verification after tests pass

Objective: Verify real runtime behavior after code-level tests pass.

Commands:

```bash
cd /Users/honbul/.hermes/hermes-agent
hermes --provider auto -m gpt-5.5 -z 'Respond exactly: AUTO_CODEX_OK'
```

Expected:

```text
AUTO_CODEX_OK
```

Then inspect latest request dump safely without printing secrets:

```bash
python - <<'PY'
from pathlib import Path
import json
root = Path('/Users/honbul/.hermes/sessions')
files = sorted(root.glob('request_dump_*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
for p in files:
    try:
        data = json.loads(p.read_text(errors='ignore'))
    except Exception:
        continue
    txt = json.dumps(data, ensure_ascii=False)
    if 'AUTO_CODEX_OK' in txt or 'gpt-5.5' in txt:
        print('file:', p)
        print('mtime:', p.stat().st_mtime)
        print('url:', data.get('url') or data.get('request', {}).get('url'))
        body = data.get('request_body') or data.get('body') or data.get('request', {}).get('body') or {}
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                body = {}
        print('model:', body.get('model'))
        print('status:', data.get('status') or data.get('response', {}).get('status'))
        break
PY
```

Expected:

- URL contains `chatgpt.com/backend-api/codex`, not `api.z.ai`.
- model is `gpt-5.5`.

Also verify explicit GLM still works if desired:

```bash
hermes --provider zai -m glm-5.1 -z 'Respond exactly: GLM_OK'
```

Expected:

```text
GLM_OK
```

---

## Task 7: Run broader focused test suite

Objective: Ensure previous Codex/delegation work still passes.

Command:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest \
  tests/tools/test_delegate.py::TestDelegationReasoningEffort \
  tests/tools/test_delegate.py::TestDispatchDelegateTask \
  tests/agent/transports/test_codex_transport.py \
  tests/hermes_cli/test_web_server.py \
  -q
```

Expected:

- Same or better than previous baseline: `160 passed` around this focused set, depending on added tests.

Also run py_compile:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m compileall hermes_cli agent tools -q
```

Expected: exit code 0.

---

## Task 8: Update memory or skill only if a durable workflow/quirk changes

Objective: Keep future sessions from repeating the same diagnosis.

If implementation confirms the fix, update memory compactly, for example:

```text
Hermes provider auto guard now maps gpt-5.5 to openai-codex before env-key fallback and fails fast on explicit provider/model mismatches such as zai+gpt-5.5.
```

Do not store tokens, account identifiers, request bodies, or credential labels.

---

## New Session Prompt

Copy-paste this into a fresh Hermes session:

```text
우리는 Hermes provider auto 라우팅 문제를 고치려고 한다. 작업 기준 repo는 /Users/honbul/.hermes/hermes-agent 이다.

배경:
- 현재 /Users/honbul/.hermes/config.yaml 은 Codex 중심으로 맞춰져 있다:
  - model.provider = openai-codex
  - model.default = gpt-5.5
  - delegation.provider = openai-codex
  - delegation.model = gpt-5.5
  - delegation.reasoning_effort = auto
- OpenAI Codex OAuth는 새 ChatGPT Pro 계정으로 다시 연결됐고, 현재 explicit/default CLI smoke는 통과했다:
  - hermes -z ... => OK
  - hermes --provider openai-codex -m gpt-5.5 -z ... => OK
- 과거 429/plus 에러는 새 OAuth refresh 이전의 old credential 또는 old runtime 상태에서 발생한 것으로 정리됐다.
- 별도 문제로, provider=auto 와 model=gpt-5.5 를 같이 쓰면 GLM_API_KEY 때문에 auto resolver가 Z.AI를 선택하고, gpt-5.5를 https://api.z.ai/api/coding/paas/v4/chat/completions 로 보내 `Unknown Model` 400을 만드는 footgun이 재현됐다.
- gateway는 이미 재시작했다.

목표:
provider=auto + model=gpt-5.5 조합이 Z.AI/GLM/Xiaomi MiMo 같은 비-Codex provider로 잘못 라우팅되지 않게 코드 레벨 guard를 추가하라.

우선 읽을 계획 파일:
/Users/honbul/.hermes/hermes-agent/docs/plans/provider-auto-guard-implementation-plan.md

작업 원칙:
1. 먼저 관련 skill을 로드하라: writing-plans, test-driven-development, systematic-debugging, 필요하면 codex/hermes-agent.
2. 절대 auth.json, .env, OAuth token, refresh token, API key 원문을 출력하지 마라. 필요하면 [REDACTED] 처리하라.
3. TDD로 진행하라. 먼저 failing test를 만들고, 그 다음 최소 구현을 하라.
4. 1차 방어: model-to-provider detection에서 gpt-5.5 같은 Codex 모델을 openai-codex로 명확히 감지하게 하라.
5. 2차 방어: explicit provider/model mismatch를 API 호출 전에 fail-fast 하라. 예: --provider zai -m gpt-5.5 는 Z.AI에 네트워크 호출을 보내지 말고 명확한 mismatch 에러를 내야 한다.
6. provider=auto + gpt-5.5 는 openai-codex로 resolve되어야 한다.
7. GLM/Z.AI와 Xiaomi MiMo의 정상 explicit 호출은 깨지면 안 된다.
8. focused tests와 compileall을 실행하고, 마지막에 실제 smoke로 `hermes --provider auto -m gpt-5.5 -z 'Respond exactly: AUTO_CODEX_OK'`를 검증하라.
9. 최신 request dump를 sanitize해서 endpoint만 확인하라. 기대 endpoint는 chatgpt.com/backend-api/codex 이고 api.z.ai가 아니어야 한다.
10. 변경 파일, 테스트 결과, 남은 리스크를 한국어로 요약하라.

예상 수정 후보 파일:
- /Users/honbul/.hermes/hermes-agent/hermes_cli/runtime_provider.py
- /Users/honbul/.hermes/hermes-agent/hermes_cli/models.py
- /Users/honbul/.hermes/hermes-agent/hermes_cli/codex_models.py
- /Users/honbul/.hermes/hermes-agent/tests/hermes_cli/test_runtime_provider.py 또는 유사 runtime provider test file

Acceptance criteria:
- `hermes --provider auto -m gpt-5.5 -z 'Respond exactly: AUTO_CODEX_OK'` 가 Z.AI가 아니라 OpenAI Codex로 간다.
- `--provider zai -m gpt-5.5` 류의 명시 mismatch는 네트워크 호출 전에 clear error로 실패한다.
- `--provider openai-codex -m gpt-5.5` 는 계속 정상이다.
- 기존 delegation reasoning effort tests와 Codex transport tests가 통과한다.
```
