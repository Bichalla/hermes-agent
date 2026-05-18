---
schema: pkm-frontmatter/v1
document_id: "supertonic3-tts-adoption-plan-20260518"
title: "Supertonic 3 TTS Adoption Implementation Plan"
subtitle: "Hermes Discord VC용 로컬 한국어 TTS 후보 도입 계획"
created: "2026-05-18T23:21:08+09:00"
updated: "2026-05-18T23:35:31+09:00"
authors:
  - Hermes
owners:
  - honbul
status: draft
lifecycle: seed
document_type: plan
audience:
  - honbul
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: high
summary: >-
  Supertonic 3를 Hermes Discord voice-channel TTS 후보로 안전하게 검증하고,
  live gateway 기본값을 건드리지 않은 상태에서 benchmark → custom command provider → optional native provider 순서로 도입하는 실행 계획이다.
tags:
  - hermes
  - tts
  - discord-voice
  - supertonic3
  - local-inference
aliases:
  - Supertonic 3 도입 계획
projects:
  - Hermes Agent
areas:
  - agent-ops
  - voice-ops
resources:
  - local-tts
entities:
  people:
    - honbul
  organizations:
    - Supertone
    - Nous Research
  brands: []
  products:
    - Supertonic 3
    - Hermes Agent
  systems:
    - Discord Gateway
    - Hermes TTS Tool
sources:
  - type: user_request
    title: "상현: Supertonic 3 도입 writing plan 요청"
    url: null
    date: "2026-05-18"
  - type: official_doc
    title: "Supertonic 3 GitHub README"
    url: "https://github.com/supertone-inc/supertonic-py"
    date: "2026-05-18"
  - type: official_doc
    title: "Supertone/supertonic-3 Hugging Face model card"
    url: "https://huggingface.co/Supertone/supertonic-3"
    date: "2026-05-18"
  - type: official_doc
    title: "supertonic PyPI package metadata"
    url: "https://pypi.org/project/supertonic/"
    date: "2026-05-18"
links:
  canonical: "/Users/honbul/.hermes/hermes-agent/.hermes/plans/2026-05-18_232108-supertonic3-tts-adoption.md"
  source: null
  related:
    - "/Users/honbul/.hermes/skills/autonomous-ai-agents/hermes-agent/references/supertonic3-local-tts-20260518.md"
    - "/Users/honbul/.hermes/hermes-agent/tools/tts_tool.py"
relations:
  parent: null
  children: []
  depends_on: []
  supersedes: []
  superseded_by: null
review:
  cadence: on_change
  last_reviewed: "2026-05-18"
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

# Supertonic 3 TTS Adoption Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Supertonic 3를 Hermes Discord VC의 한국어 TTS 후보로 안전하게 도입하되, 현재 작동 중인 Edge TTS/gateway 기본 경로를 먼저 깨지 않는다.

**Architecture:** 1차는 live gateway와 분리된 isolated benchmark로 품질·latency·format을 검증한다. 2차는 Hermes의 기존 `tts.providers.<name>` custom command provider 구조로 `supertonic3`를 붙이고, 충분히 안정화된 뒤에만 native provider 편입을 검토한다.

**Tech Stack:** Hermes Agent Python 3.11 venv, `tools/tts_tool.py`, custom command TTS provider, `supertonic==1.3.1`, ONNX Runtime, Hugging Face model assets, Discord gateway voice playback.

---

## Policy block

```yaml
risk_class: S2
policy_source: /Users/honbul/.hermes/docs/hermes-autonomous-recursive-improvement-policy-20260429.md
required_reviews: [engineering-plan-review, operations-safety-review]
execution_allowed: false
measurement_source: isolated_supertonic3_benchmark_json
public_surface_policy: none
forbidden_without_confirmation:
  - gateway_restart_during_active_voice_session
  - switching_live_tts_provider
  - public_deploy
  - paid_action
  - credentials
  - DNS
  - irreversible_delete
```

Execution remains locked because the current request is plan-only. Implementation requires a later explicit, scope-bound proceed signal. Until then, install, model download, benchmark execution, live config edit, gateway restart, and provider switch are all forbidden.

---

## Official documentation read first

The plan is based on official project/package/model sources checked before writing:

1. **Supertonic GitHub README** — `https://github.com/supertone-inc/supertonic-py`
   - Describes Supertonic 3 as “Lightning Fast, On-Device TTS”.
   - States Supertonic 3 supports **31 languages** plus `na` fallback.
   - Shows install command: `pip install supertonic`.
   - Shows Python API: `from supertonic import TTS`, `tts.get_voice_style(voice_name="M1")`, `tts.synthesize(..., lang="ko")`, `tts.save_audio(...)`.
   - Shows CLI command: `supertonic tts '...' -o output.wav --lang ko`.
   - Notes first run downloads model assets of roughly **400MB** into local cache.
   - Lists built-in voices `M1–M5`, `F1–F5`.
   - Lists requirements: `onnxruntime`, `numpy`, `soundfile`, `huggingface-hub`.

2. **Hugging Face model card** — `https://huggingface.co/Supertone/supertonic-3`
   - States Supertonic is lightweight local inference, runs with ONNX Runtime, and needs no cloud synthesis call.
   - States Supertonic 3 expands from 5 to **31 languages**, improves reading stability, and reduces repeat/skip failures.
   - Lists Korean support: `ko` = Korean.
   - States public ONNX assets are about **99M parameters**.
   - License section says sample code is MIT and the model is OpenRAIL-M.

3. **PyPI package metadata** — `https://pypi.org/project/supertonic/`
   - Package name: `supertonic`.
   - Version observed: `1.3.1`.
   - Summary: “High-quality Text-to-Speech synthesis with ONNX Runtime”.
   - Dependencies include `onnxruntime`, `numpy`, `soundfile`, `huggingface-hub`.
   - Optional extras include playback/server/dev dependencies.

4. **Hugging Face license** — `https://huggingface.co/Supertone/supertonic-3/blob/main/LICENSE`
   - License: BigScience OpenRAIL-M.
   - Permits running/evaluating/using the model subject to use restrictions.
   - Does not grant unrestricted misuse rights; avoid impersonation/deepfake use without consent and keep AI-generated disclosure discipline for published outputs.

---

## Verified context inventory

| Source | Verified facts |
|---|---|
| `/Users/honbul/.hermes/hermes-agent/tools/tts_tool.py:1563-1781` | TTS provider is resolved from config; command providers resolve before built-in dispatch; output extension can follow command provider config; command providers can be `voice_compatible`; generated audio is verified non-empty; Opus conversion path exists for voice-compatible audio. |
| `/Users/honbul/.hermes/hermes-agent/tools/tts_tool.py:1684-1708` | Native local providers currently include `kittentts` and `piper`; no native `supertonic` dispatch branch was observed. |
| `/Users/honbul/.hermes/hermes-agent/tests/tools/test_tts_command_providers.py` | Existing focused tests cover custom command provider resolution, output format, timeout, `voice_compatible`, max text length, and `text_to_speech_tool` behavior without real TTS engines. |
| `/Users/honbul/.hermes/hermes-agent/tests/tools/test_tts_kittentts.py` | Native local TTS provider tests stub external modules and assert cache/config/output behavior. This is the pattern to copy if native Supertonic provider is added later. |
| `/Users/honbul/.hermes/hermes-agent/tests/tools/test_tts_piper.py` | Native Piper tests cover registration, import probes, model/voice resolution, cache reuse, and dispatcher behavior. |
| `/Users/honbul/.hermes/hermes-agent/tests/gateway/test_tts_media_routing.py` | Gateway media routing distinguishes plain audio attachments from voice-tagged audio. This matters for Discord/Telegram voice-bubble compatibility. |
| Safe config inspection from current session | Current `tts.provider` is `edge`; no custom command TTS providers are configured. Do not print raw config because it may contain secrets. |
| `pip install --dry-run supertonic==1.3.1` from current session | Dry-run succeeds in Hermes venv; would install `supertonic-1.3.1` and `soundfile-0.13.1`; existing deps include `onnxruntime`, `numpy`, `huggingface-hub`. |
| Hugging Face asset HEAD checks from current session | Public ONNX assets total roughly `379.9 MB`. |
| `git status --short --branch` | Repo was clean at plan-writing time: `## main...origin/main`. |

---

## Non-goals

- Do not restart the gateway during an active Discord VC session.
- Do not switch `tts.provider` from `edge` to `supertonic3` until benchmark evidence exists.
- Do not commit or print secrets from `/Users/honbul/.hermes/config.yaml` or `/Users/honbul/.hermes/.env`.
- Do not use custom voice cloning for the commute MVP unless consent/provenance is explicit.
- Do not treat Supertonic 3 as “production default” until warm latency and Discord playback are verified.

---

## Acceptance criteria

1. Official docs have been checked and are cited in the plan.
2. Isolated local smoke generates a Korean WAV without touching gateway config.
3. Benchmark records cold-start time, warm generation time, audio duration, file format, file size, and success/failure into a JSON artifact.
4. A custom command provider path can synthesize via Hermes `text_to_speech_tool` in a temp/test `HERMES_HOME` without modifying live config.
5. Live provider switch remains blocked unless benchmark gates pass: warm short-answer synthesis latency `<= 3.0s`, warm RTF `<= 1.0`, Discord playback-start latency `<= 2.0s` after audio file exists, zero TTS command failures in at least 5 consecutive short Korean samples, and a human spot-check rates Korean intelligibility/naturalness as acceptable versus current Edge output. If any hard gate fails, keep `tts.provider: edge`.
6. If live config is changed later, the old `edge` provider remains documented as rollback.
7. Gateway restart happens only after user approval and only when active-session preflight proves: no active Discord VC playback, no active STT/voice turn, no in-flight gateway agent turn for the target Discord thread, and no gateway drain/restart already in progress. If any source is unavailable or ambiguous, fail closed and do not restart/switch.
8. All focused tests pass before any commit/push of code changes.
9. Any plan-created Markdown and any durable docs include PKM frontmatter.

---

## Proposed implementation strategy

Use three phases:

1. **Phase A — Isolated benchmark only**
   - Install package in venv.
   - Generate Korean sample to `/tmp` or `~/.hermes/reports/...`.
   - Record cold/warm metrics.
   - Do not modify live Hermes config.

2. **Phase B — Custom command provider**
   - Add a small wrapper script that reads `{input_path}` and writes `{output_path}`.
   - Configure a temp/test `HERMES_HOME` first.
   - Only after temp test passes, add live config entry while keeping `tts.provider: edge` unchanged, or switch only with explicit approval.

3. **Phase C — Optional native provider**
   - If command-provider overhead or reliability is poor, add native `supertonic` provider in `tools/tts_tool.py` using the same patterns as Piper/KittenTTS.
   - This phase needs TDD and commit/push because it changes repo code.

---

## Task 1: Confirm package/runtime readiness without installing

**Objective:** Re-run a read-only dependency and version probe so implementation starts from fresh evidence.

**Files:**
- Read: `/Users/honbul/.hermes/hermes-agent/venv`
- No source file changes.

**Step 1: Run package/import probe**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python - <<'PY'
import importlib.util, platform, sys
mods = ['supertonic', 'onnxruntime', 'soundfile', 'huggingface_hub']
print('python', sys.version.split()[0], platform.system(), platform.machine())
for name in mods:
    print(name, bool(importlib.util.find_spec(name)))
PY
```

Expected: Python info prints; `onnxruntime` and `huggingface_hub` should be available; `supertonic` may be absent before installation.

**Step 2: Re-run dry-run install**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pip install --dry-run supertonic==1.3.1
```

Expected: dry-run exits 0 and does not show dependency conflicts.

**Step 3: Record evidence**

Save only non-secret output summary in the eventual benchmark report. Do not commit environment logs unless redacted.

---

## Task 2: Install Supertonic package in Hermes venv

**Objective:** Install the official SDK dependency needed for local synthesis.

**Files:**
- Mutates: `/Users/honbul/.hermes/hermes-agent/venv/` package environment only.
- No repo file changes.

**Step 1: Install pinned package**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pip install supertonic==1.3.1
```

Expected: installs `supertonic` and `soundfile` if missing.

**Step 2: Verify CLI entrypoint**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/supertonic --help
```

Expected: help lists `tts`, `say`, `serve`, and related commands.

**Step 3: Verify import**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python - <<'PY'
from supertonic import TTS
print('SUPER TONIC IMPORT OK', TTS)
PY
```

Expected: prints import success without downloading model yet, unless package behavior changes.

---

## Task 3: Run isolated Korean smoke generation

**Objective:** Generate one Korean sample outside Hermes gateway to verify functional synthesis and output format.

**Files:**
- Create: `/tmp/supertonic3-smoke.wav`
- No source file changes.

**Step 1: Generate WAV through official CLI**

```bash
cd /Users/honbul/.hermes/hermes-agent
time venv/bin/supertonic tts '혼불아, 출근길 음성 테스트입니다.' \
  -o /tmp/supertonic3-smoke.wav \
  --model supertonic-3 \
  --voice M1 \
  --lang ko \
  --steps 8 \
  --speed 1.05
```

Expected:
- First run may download about 400MB model assets.
- Command exits 0.
- `/tmp/supertonic3-smoke.wav` exists and is non-empty.

**Step 2: Inspect output file**

```bash
file /tmp/supertonic3-smoke.wav
python3 - <<'PY'
from pathlib import Path
p = Path('/tmp/supertonic3-smoke.wav')
print('exists', p.exists())
print('size', p.stat().st_size if p.exists() else 0)
PY
```

Expected: WAV audio file, non-zero size.

**Step 3: Failure handling**

If generation fails:
- Capture command, exit code, and last 40 lines of stderr only.
- Do not proceed to Hermes integration.
- Check whether failure is model download, package import, unsupported character, or audio write dependency.

---

## Task 4: Benchmark cold and warm generation

**Objective:** Determine whether Supertonic 3 latency is acceptable for commute voice interaction.

**Files:**
- Create: `/Users/honbul/.hermes/reports/supertonic3-benchmark-YYYYMMDD.json`
- Create: `/Users/honbul/.hermes/reports/supertonic3-benchmark-YYYYMMDD.md` with PKM frontmatter if a human-readable report is produced.

**Step 1: Write benchmark runner to a temp file**

Use a temp script first; only promote it to repo/scripts if repeated use is needed.

```bash
cat > /tmp/supertonic3_bench.py <<'PY'
import json, time, wave
from pathlib import Path
from supertonic import TTS

samples = [
    '혼불아, 오늘 JÖKL 마케팅 체크리스트를 짧게 정리해줘.',
    '회의는 잠시 후에 시작되며 모두가 자리에 앉아 기다립니다.',
]
out_dir = Path('/tmp/supertonic3-bench')
out_dir.mkdir(parents=True, exist_ok=True)

result = {'runs': []}
t0 = time.perf_counter()
tts = TTS(model='supertonic-3')
result['model_load_seconds'] = round(time.perf_counter() - t0, 3)
style = tts.get_voice_style(voice_name='M1')

for idx, text in enumerate(samples, 1):
    out = out_dir / f'run-{idx}.wav'
    t1 = time.perf_counter()
    wav, duration = tts.synthesize(text, voice_style=style, lang='ko', total_steps=8, speed=1.05)
    synth_s = time.perf_counter() - t1
    tts.save_audio(wav, str(out))
    with wave.open(str(out), 'rb') as wf:
        audio_seconds = wf.getnframes() / float(wf.getframerate())
        sample_rate = wf.getframerate()
    result['runs'].append({
        'text_chars': len(text),
        'synth_seconds': round(synth_s, 3),
        'audio_seconds': round(audio_seconds, 3),
        'rtf': round(synth_s / audio_seconds, 3) if audio_seconds else None,
        'sample_rate': sample_rate,
        'output': str(out),
        'bytes': out.stat().st_size,
    })
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
```

**Step 2: Run benchmark and persist durable JSON**

```bash
cd /Users/honbul/.hermes/hermes-agent
REPORT_DATE=$(date +%Y%m%d)
REPORT_JSON=/Users/honbul/.hermes/reports/supertonic3-benchmark-${REPORT_DATE}.json
mkdir -p /Users/honbul/.hermes/reports
venv/bin/python /tmp/supertonic3_bench.py | tee /tmp/supertonic3-benchmark.json
cp /tmp/supertonic3-benchmark.json "$REPORT_JSON"
python3 - <<'PY' "$REPORT_JSON"
from pathlib import Path
import json, sys
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding='utf-8'))
assert 'model_load_seconds' in data
assert data.get('runs'), 'missing benchmark runs'
for run in data['runs']:
    assert run.get('bytes', 0) > 0
    assert run.get('audio_seconds', 0) > 0
print('BENCHMARK_JSON_VALID', p)
PY
```

Expected: JSON with `model_load_seconds`, per-run `synth_seconds`, `audio_seconds`, and `rtf`; durable copy exists under `/Users/honbul/.hermes/reports/`.

**Step 3: Decide pass/fail**

Hard pass threshold for considering any later live Discord switch:
- Warm short-answer synthesis latency must be `<= 3.0s` for each benchmarked short Korean sample.
- Warm RTF must be `<= 1.0`.
- The direct wrapper/tool path must show zero TTS command failures across at least 5 consecutive short Korean samples.
- If Discord playback is tested in a later approved safe window, playback should start within `<= 2.0s` after the audio file exists.
- Korean intelligibility/naturalness must be acceptable in a human spot-check against the current Edge output.
- Cold-start can be slower if gateway prewarm is later added, but cold download/load must not occur during a live voice turn.
- If any hard gate fails, do not switch live `tts.provider`; keep `edge` as default and record the blocker.

---

## Task 5: Create custom command wrapper script

**Objective:** Provide a stable Hermes command-provider boundary that reads text from file and writes WAV output.

**Files:**
- Create: `/Users/honbul/.hermes/scripts/supertonic3_tts_command.py`
- Optional Test: a local smoke command in a temp `HERMES_HOME`; no repo code changes yet.

**Step 1: Create wrapper script**

```python
#!/usr/bin/env python3
"""Hermes custom command TTS wrapper for Supertonic 3."""
import argparse
from pathlib import Path
from supertonic import TTS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--model', default='supertonic-3')
    parser.add_argument('--voice', default='M1')
    parser.add_argument('--lang', default='ko')
    parser.add_argument('--steps', type=int, default=8)
    parser.add_argument('--speed', type=float, default=1.05)
    parser.add_argument('--max-chunk-length', type=int, default=None)
    parser.add_argument('--silence-duration', type=float, default=0.3)
    args = parser.parse_args()

    text = Path(args.input).read_text(encoding='utf-8').strip()
    if not text:
        raise SystemExit('input text is empty')

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    tts = TTS(model=args.model)
    style = tts.get_voice_style(voice_name=args.voice)
    wav, _duration = tts.synthesize(
        text,
        voice_style=style,
        total_steps=args.steps,
        speed=args.speed,
        max_chunk_length=args.max_chunk_length,
        silence_duration=args.silence_duration,
        lang=args.lang,
    )
    tts.save_audio(wav, str(output))
    if not output.exists() or output.stat().st_size == 0:
        raise SystemExit(f'no output written: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

**Step 2: Smoke wrapper directly**

```bash
printf '혼불아, Supertonic 3 wrapper 테스트입니다.' > /tmp/supertonic3-input.txt
/Users/honbul/.hermes/hermes-agent/venv/bin/python \
  /Users/honbul/.hermes/scripts/supertonic3_tts_command.py \
  --input /tmp/supertonic3-input.txt \
  --output /tmp/supertonic3-wrapper.wav \
  --voice M1 --lang ko --steps 8 --speed 1.05
file /tmp/supertonic3-wrapper.wav
```

Expected: non-empty WAV file.

---

## Task 6: Test Hermes custom command provider in isolated HERMES_HOME

**Objective:** Verify Hermes can call the wrapper through `text_to_speech_tool` without touching live config.

**Files:**
- Create: `/tmp/hermes-supertonic3-test-home/config.yaml`
- No live config changes.

**Step 1: Create temp config**

```bash
rm -rf /tmp/hermes-supertonic3-test-home
mkdir -p /tmp/hermes-supertonic3-test-home
cat > /tmp/hermes-supertonic3-test-home/config.yaml <<'YAML'
tts:
  provider: supertonic3
  providers:
    supertonic3:
      type: command
      command: "/Users/honbul/.hermes/hermes-agent/venv/bin/python /Users/honbul/.hermes/scripts/supertonic3_tts_command.py --input {input_path} --output {output_path} --voice M1 --lang ko --steps 8 --speed 1.05"
      output_format: wav
      timeout_seconds: 90
      voice_compatible: true
      max_text_length: 1200
YAML
```

**Step 2: Call tool directly with temp home**

```bash
cd /Users/honbul/.hermes/hermes-agent
HERMES_HOME=/tmp/hermes-supertonic3-test-home venv/bin/python - <<'PY'
import json
from tools.tts_tool import text_to_speech_tool
res = json.loads(text_to_speech_tool('혼불아, Hermes custom command provider 테스트입니다.'))
print(json.dumps({k: res.get(k) for k in ['success', 'provider', 'file_path', 'voice_compatible']}, ensure_ascii=False, indent=2))
if not res.get('success'):
    raise SystemExit(res)
PY
```

Expected:
- `success: true`
- `provider: supertonic3`
- output file exists
- `voice_compatible` may become true if Opus conversion succeeds; if false, inspect ffmpeg/format path before live VC use.

---

## Task 7: Add focused automated tests if repo code changes are required

**Objective:** Keep TDD discipline if moving beyond config/wrapper into source-code changes.

**Files:**
- Test: `/Users/honbul/.hermes/hermes-agent/tests/tools/test_tts_supertonic.py`
- Modify: `/Users/honbul/.hermes/hermes-agent/tools/tts_tool.py`

**When to do this task:** Only if Phase C native provider is chosen. If custom command provider is sufficient, skip this task.

**Step 1: Write failing tests first**

Copy patterns from `tests/tools/test_tts_kittentts.py` and `tests/tools/test_tts_piper.py`.

Minimum tests:
- `supertonic` is included in `BUILTIN_TTS_PROVIDERS`.
- `_check_supertonic_available()` returns boolean without raising.
- `_generate_supertonic_tts()` loads `TTS(model='supertonic-3')`, gets `M1`, calls `synthesize(..., lang='ko')`, saves WAV.
- model instance cache is reused.
- dispatcher returns helpful error when package is missing.

**Step 2: Run failing tests**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest tests/tools/test_tts_supertonic.py -q -o 'addopts='
```

Expected: FAIL before implementation.

**Step 3: Implement minimal native provider**

Implementation outline:
- Add constants: `DEFAULT_SUPERTONIC_MODEL = 'supertonic-3'`, `DEFAULT_SUPERTONIC_VOICE = 'M1'`.
- Add `_supertonic_model_cache`.
- Add `_import_supertonic()` and `_check_supertonic_available()`.
- Add `_generate_supertonic_tts(text, output_path, tts_config)`.
- Add dispatch branch before default Edge fallback.
- Add provider text length cap.

**Step 4: Run focused tests**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest \
  tests/tools/test_tts_supertonic.py \
  tests/tools/test_tts_command_providers.py \
  tests/tools/test_tts_kittentts.py \
  tests/tools/test_tts_piper.py \
  -q -o 'addopts='
```

Expected: all pass.

---

## Task 8: Prepare live config change as a reversible patch

**Objective:** Make the live config change narrow, reversible, and not secret-leaking. This task is forbidden until 상현 explicitly approves live config modification after Phase A/B evidence passes.

**Files:**
- Modify carefully: `/Users/honbul/.hermes/config.yaml`
- Do not print raw file contents.

**Step 0: Require explicit live-config approval**

Before any edit to `/Users/honbul/.hermes/config.yaml`, obtain a scope-bound approval such as:

```text
Approve live config edit only: add dormant supertonic3 provider entry, keep tts.provider=edge, do not restart gateway.
```

If approval is absent, stop here. Adding a dormant provider entry is still a live config mutation.

**Step 1: Backup config locally**

```bash
cp /Users/honbul/.hermes/config.yaml /Users/honbul/.hermes/config.yaml.pre-supertonic3.$(date +%Y%m%d%H%M%S).bak
```

**Step 2: Add provider entry without switching default**

Add this non-secret shape under `tts.providers`, preserving existing config:

```yaml
tts:
  providers:
    supertonic3:
      type: command
      command: "/Users/honbul/.hermes/hermes-agent/venv/bin/python /Users/honbul/.hermes/scripts/supertonic3_tts_command.py --input {input_path} --output {output_path} --voice M1 --lang ko --steps 8 --speed 1.05"
      output_format: wav
      timeout_seconds: 90
      voice_compatible: true
      max_text_length: 1200
```

Keep `tts.provider: edge` until explicit approval to switch.

**Step 3: Validate config shape without printing secrets**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python - <<'PY'
from pathlib import Path
import yaml
cfg = yaml.safe_load(Path('/Users/honbul/.hermes/config.yaml').read_text()) or {}
tts = cfg.get('tts') or {}
providers = tts.get('providers') or {}
print('tts_provider', tts.get('provider'))
print('has_supertonic3', 'supertonic3' in providers)
print('supertonic3_keys', sorted((providers.get('supertonic3') or {}).keys()))
PY
```

Expected: provider still `edge`, `has_supertonic3 true`, keys only printed.

---

## Task 9: Controlled gateway smoke after approval

**Objective:** Verify Discord VC playback only after local tests pass, benchmark gates pass, live config approval is recorded, and the user approves a safe restart/switch window.

**Files:**
- No source changes.
- Runtime action: gateway restart only after approval.

**Step 1: Preflight active voice state**

Use Hermes logs/status scanners, not raw secret dumps. Confirm all of the following before any restart/switch:

- No active Discord VC playback for the target guild/channel.
- No active STT capture or voice turn for the target user/thread.
- No in-flight gateway agent turn for the target Discord thread.
- Gateway is not already draining, stopping, or restarting.
- The last relevant TTS/playback events do not show an unresolved timeout/error.

Record the preflight result as a small redacted artifact or command summary. If any condition is ambiguous, fail closed and do not restart/switch.

**Step 2: Switch provider only if approved**

Only if 상현 explicitly approves live switch and the benchmark/preflight gates above passed:

```bash
hermes config set tts.provider supertonic3
```

**Step 3: Restart gateway in safe window**

```bash
cd /Users/honbul/.hermes/hermes-agent
hermes gateway restart && sleep 3 && hermes gateway status
```

Expected: gateway reconnects; no active turn interrupted.

**Step 4: VC smoke script**

In Discord VC, ask a short wake-word command:

```text
혼불아, 짧게 음성 테스트라고 말해줘.
```

Expected:
- STT receives only the intended utterance.
- Hermes response is short.
- TTS playback begins within acceptable warm latency.
- No 120s playback timeout appears.

---

## Task 10: Rollback path

**Objective:** Ensure Supertonic can be disabled quickly if latency/quality is poor.

**Files:**
- Modify: `/Users/honbul/.hermes/config.yaml`
- Runtime: gateway restart only in safe window.

**Step 1: Switch back to Edge**

```bash
hermes config set tts.provider edge
```

**Step 2: Restart gateway safely**

```bash
cd /Users/honbul/.hermes/hermes-agent
hermes gateway restart && sleep 3 && hermes gateway status
```

**Step 3: Verify provider summary safely**

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python - <<'PY'
from pathlib import Path
import yaml
cfg = yaml.safe_load(Path('/Users/honbul/.hermes/config.yaml').read_text()) or {}
print('tts_provider', (cfg.get('tts') or {}).get('provider'))
PY
```

Expected: `tts_provider edge`.

---

## Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| First-run model download stalls live VC | User hears silence / timeout | Download and benchmark before gateway switch. |
| Cold model load too slow | Conversation feels broken | Prewarm later or keep Edge default. |
| Korean pronunciation/naturalness not good enough | Poor commute UX | Compare against Edge on identical phrases before default switch. |
| Command provider process startup overhead | Warm latency worse than native | Promote to native provider only if benchmark proves command overhead is material. |
| OpenRAIL-M use restrictions | Compliance risk for public outputs/custom voices | Internal test only; no impersonation; disclose AI-generated published audio. |
| Config edit leaks secrets | Security incident | Never print raw config/env; only print key names and booleans. |
| Gateway restart interrupts active voice turn | User loses live session | Require approval/safe window before restart. |

---

## Open questions

1. What warm latency threshold is acceptable to 상현 for commute use: under 2s, 3s, or 5s?
2. Should the initial voice be `M1`, `F1`, or should we A/B built-in voices after the first smoke?
3. Should Supertonic be used only for Discord VC, or also for generic Hermes TTS attachments?
4. If quality is good but cold start is slow, should we add gateway prewarm later?

---

## Review evidence

- 2026-05-18 initial structural validation: **PASS**.
- 2026-05-18 independent engineering plan review: **REQUEST_CHANGES**. Blocking findings: durable benchmark JSON path mismatch; live config mutation needed an explicit approval gate before any edit.
- 2026-05-18 independent operations/safety review: **REQUEST_CHANGES**. Blocking findings: live-switch benchmark gates too loose; Discord VC active-session preflight too vague; Phase A approval scope ambiguous.
- 2026-05-18 patch applied: durable report JSON write/validation added; explicit live-config approval gate added; hard benchmark/live-switch thresholds added; Discord VC preflight made fail-closed and concrete; execution lock clarified to forbid install/download/benchmark/config/restart/switch before scope-bound approval.
- 2026-05-18 focused engineering re-review: **PASS**. Prior blockers resolved; no new blocking engineering issues found.
- 2026-05-18 focused operations/safety re-review: **PASS**. Prior blockers resolved; no new blocking operations/safety issues found.
- Gate status: **ENGINEERING PASS + OPERATIONS/SAFETY PASS**. Implementation remains locked until explicit, scope-bound approval.

---

## Execution handoff

Plan saved only. No implementation, install, config mutation, gateway restart, or live provider switch has been performed by this plan-writing step.

Recommended scope-bound approval phrase if 상현 wants Phase A execution later:

```text
Proceed with Phase A only: install supertonic==1.3.1 in Hermes venv, allow first-run model download, generate isolated Korean smoke WAV, write benchmark JSON under /Users/honbul/.hermes/reports/, and report results. Do not edit live config, do not switch tts.provider, and do not restart gateway.
```
