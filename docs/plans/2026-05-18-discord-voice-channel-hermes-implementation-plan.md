---
schema: pkm-frontmatter/v1
document_id: "discord-voice-channel-hermes-implementation-plan-20260518"
title: "Discord Voice Channel Hermes Implementation Plan"
subtitle: "출근길 음성 채널 동석형 Hermes 업무 비서 MVP 구현 계획"
created: "2026-05-18T08:05:32+09:00"
updated: "2026-05-18T08:36:00+09:00"
authors:
  - Hermes
owners:
  - honbul
status: active
lifecycle: sprout
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
  Discord 음성 채널에 Hermes 봇이 참여해 사용자의 발화를 STT로 처리하고 TTS로 응답하는 출근길 업무 비서 MVP를 구현·검증하기 위한 policy-compliant Writing Plan이다.
  기존 Hermes 코드에 이미 있는 Discord VC 수신/재생 경로를 의존성, 설정, 테스트, 실전 스모크, 운영 UX로 안정화한다.
tags:
  - hermes
  - discord
  - voice-channel
  - stt
  - tts
  - implementation-plan
aliases:
  - Hermes Discord VC plan
  - 출근길 음성 Hermes 계획
projects:
  - Hermes Agent
areas:
  - agent-ops
  - communication
  - discord-ops
resources: []
entities:
  people:
    - honbul
  organizations:
    - NousResearch
  brands:
    - JÖKL
  products:
    - Hermes Agent
  systems:
    - Discord Gateway
    - Hermes Gateway
sources:
  - type: user_request
    title: "Discord 음성 채널에서 Hermes와 소통 가능한지 및 구현계획서 요청"
    url: null
    date: "2026-05-18"
links:
  canonical: /Users/honbul/.hermes/hermes-agent/docs/plans/2026-05-18-discord-voice-channel-hermes-implementation-plan.md
  source: null
  related:
    - /Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py
    - /Users/honbul/.hermes/hermes-agent/gateway/run.py
    - /Users/honbul/.hermes/hermes-agent/tests/gateway/test_voice_command.py
    - /Users/honbul/.hermes/hermes-agent/tests/integration/test_voice_channel_flow.py
relations:
  parent: null
  children: []
  depends_on:
    - /Users/honbul/.hermes/docs/hermes-autonomous-recursive-improvement-policy-20260429.md
    - /Users/honbul/.hermes/docs/document-frontmatter-standard.md
  supersedes: []
  superseded_by: null
review:
  cadence: on_change
  last_reviewed: "2026-05-18"
  next_review: null
governance:
  pkm_required: true
  frontmatter_required: true
  approval_required_for_external_publish: false
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
version: 1.0.0
---

# Discord Voice Channel Hermes Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after the review gates recorded below pass.

**Goal:** Discord 음성 채널에 Hermes가 동석해 상현의 발화를 듣고, 기존 Hermes agent pipeline으로 처리한 뒤, 짧은 음성 응답과 텍스트 로그를 남기는 출근길 업무 비서 MVP를 안정적으로 활성화한다.

**Architecture:** 기존 `gateway.platforms.discord.DiscordAdapter`의 voice channel join/listen/playback 경로를 보존하고, 누락된 런타임 의존성·권한·설정·테스트·운영 UX를 보강한다. 구현은 “기존 경로 활성화 → fail-closed preflight → 한국어 STT/TTS 품질 → Discord 실전 smoke → 운영 runbook” 순서로 진행하며, Discord VC 내부 API 의존 위험은 명시적 canary와 rollback으로 통제한다.

**Tech Stack:** Python 3.11, Hermes Agent, `discord.py[voice]`, PyNaCl, davey, libopus, ffmpeg, faster-whisper local STT, Edge TTS, pytest, Discord slash commands.

---

## Policy block

```yaml
risk_class: S3
policy_source: /Users/honbul/.hermes/docs/hermes-autonomous-recursive-improvement-policy-20260429.md
required_reviews:
  - plan-eng-review
  - plan-ops-ceo-review
execution_allowed: true
execution_scope: dependency_preflight_and_discord_vc_mvp_only
measurement_source: tests + gateway logs + manual Discord VC smoke transcript + TTS playback confirmation
public_surface_policy: none
forbidden_without_confirmation:
  - public_deploy
  - paid_action
  - credentials
  - DNS
  - irreversible_delete
  - Discord permission widening beyond the existing JÖKL/Hermes server scope
```

## Review evidence

Status at draft creation: `PENDING`.

Review loop:

- 2026-05-18 `plan-eng-review`: `REQUEST_CHANGES` — required macOS/Homebrew libopus fallback in preflight, exact Discord VC permission-gap task, and tool-discipline-safe log scan command.
- 2026-05-18 `plan-ops-ceo-review`: `REQUEST_CHANGES` — required participant consent/privacy boundary, linked text channel visibility boundary, and raw-audio retention rule.
- 2026-05-18 patch v0.2.0: addressed both reviewers' blockers; focused re-review required before execution.
- 2026-05-18 `plan-ops-ceo-review` focused re-review: `PASS`.
- 2026-05-18 `plan-eng-review` focused re-review: `REQUEST_CHANGES` — requested explicit missing `Speak` and both-missing `Connect` + `Speak` test/acceptance coverage.
- 2026-05-18 patch v0.3.0: added explicit `Speak` and both-missing permission-gap tests plus acceptance wording; focused eng re-review required before execution.
- 2026-05-18 `plan-eng-review` second focused re-review: `PASS`.
- 2026-05-18 structural validation: `PASS` for v0.3.0 before final unlock.
- 2026-05-18 final status: `ALL_REQUIRED_REVIEWS_PASS`; `execution_allowed` set to `true` for `dependency_preflight_and_discord_vc_mvp_only` only.

## Verified context inventory

| Source | Verified fact |
|---|---|
| `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:121-190` | `VoiceReceiver` exists and is designed to capture/decrypt/decode Discord VC audio, buffer per-user audio, and detect completed utterances. |
| `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:539-562` | Discord adapter attempts to load libopus during `connect()` and logs `Opus codec not found — voice channel playback disabled` if missing. |
| `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:595-604` | Discord bot requests `message_content`, DM/guild messages, optional members, and `voice_states` intents. |
| `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:1474-1490` | `play_tts()` routes TTS to VC via `play_in_voice_channel()` when the bot is connected to a bound guild voice channel; otherwise sends Discord audio file. |
| `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:1583-1615` | `join_voice_channel()` connects the bot, starts `VoiceReceiver`, and starts `_voice_listen_loop()`. |
| `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:1640-1681` | `play_in_voice_channel()` pauses receiver for echo prevention, uses `discord.FFmpegPCMAudio`, waits for playback completion, and resumes receiver. |
| `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:1807-1872` | `_voice_listen_loop()` checks completed utterances, runs PCM→WAV→STT through `transcribe_audio()`, filters hallucinations, and calls `_voice_input_callback`. |
| `/Users/honbul/.hermes/hermes-agent/gateway/run.py:7733-7860` | `/voice` supports `on`, `off`, `tts`, `channel`, `leave`, `status`; `/voice channel` joins the user's current Discord VC and sets voice mode to `all`. |
| `/Users/honbul/.hermes/hermes-agent/gateway/run.py:7894-7952` | Transcribed voice input is converted to a synthetic `MessageEvent` with `MessageType.VOICE` and passed through normal adapter message handling. |
| `/Users/honbul/.hermes/hermes-agent/gateway/run.py:8007-8055` | Auto voice replies use `text_to_speech_tool`; if the bot is in a VC, audio is played in the VC instead of sent as a file. |
| `/Users/honbul/.hermes/hermes-agent/tests/gateway/test_voice_command.py` | Existing test coverage includes `/voice` modes, Discord VC join/leave/input, `VoiceReceiver`, `play_tts`, `play_in_voice_channel`, and playback timeout behavior. |
| `/Users/honbul/.hermes/hermes-agent/tests/integration/test_voice_channel_flow.py` | Existing integration tests cover `VoiceReceiver` audio-flow behavior without requiring a live Discord connection. |
| Runtime probe, 2026-05-18 | In the active environment: `discord`, `nacl`, `davey`, `edge_tts`, and `faster_whisper` are importable; `mutagen`, `ffmpeg`, and `libopus` are missing from the probed shell environment. |
| `/Users/honbul/.hermes/logs/gateway.log` | Prior gateway runs logged `Opus codec not found — voice channel playback disabled`. |
| `/Users/honbul/.hermes/config.yaml` | `voice.auto_tts: false`; `stt.enabled: true`, `stt.provider: local`, `stt.local.model: base`; `tts.provider: edge`, current Edge voice `en-US-AriaNeural`. |
| `pyproject.toml:46,55` | Project optional dependencies include `discord.py[voice]` and `faster-whisper`; system packages like ffmpeg/libopus remain external runtime dependencies. |

## Scope

### In scope

- Enable and verify existing Discord VC path on the current Hermes gateway.
- Add fail-closed preflight/reporting for missing VC dependencies where useful.
- Tune Korean STT/TTS defaults for this use case without changing global provider credentials.
- Add or adjust tests only where current behavior is untested or brittle.
- Write a short runbook for starting/stopping/validating VC mode.
- Run a manual Discord VC smoke test in the current server/thread context.

### Out of scope

- Building a separate Discord bot from scratch.
- Replacing Hermes gateway architecture.
- Multi-speaker meeting transcription/diarization beyond current per-user SSRC mapping.
- Wake-word/barge-in/interrupt support beyond current `/voice channel` command flow.
- Paid STT/TTS provider integration unless explicitly approved.
- Public deploy, DNS, external product changes, or JÖKL customer-facing changes.

## Acceptance criteria

1. `ffmpeg` and libopus are installed/discoverable in the gateway runtime environment.
2. Focused voice tests pass:
   - `venv/bin/python -m pytest tests/gateway/test_voice_command.py -q -o 'addopts='`
   - `venv/bin/python -m pytest tests/integration/test_voice_channel_flow.py -q -o 'addopts='`
3. A VC dependency preflight command produces a clear PASS/FAIL result without printing secrets.
4. `/voice channel` joins the user's current Discord VC in the JÖKL server.
5. `/voice status` reports the connected VC and participants.
6. A Korean test utterance appears in the linked text channel as `**[Voice]** <@user>: ...` with acceptable transcription quality.
7. Hermes processes the utterance through the normal Discord session and returns a short response.
8. The response is played in the VC and, where normal gateway behavior sends text, text remains visible in the linked channel/thread for auditability.
10. `/voice leave` disconnects and disables VC mode for that chat.
11. Gateway logs contain no recurring traceback, Opus load warning, or ffmpeg playback failure during the smoke test.
12. Rollback is documented and tested at least to the extent of `/voice leave` + config restoration + gateway restart.
13. Implementation artifacts contain no raw audio and no sensitive transcript beyond sanitized snippets.
14. VC permission preflight fails closed and reports exact missing Discord VC permission names: `Connect`, `Speak`, or both.

## Fail-closed rules

- If `ffmpeg` or libopus is missing after installation, do not start the live smoke test.
- If Discord bot lacks `Connect` or `Speak` in the target VC, stop and report the exact permission gap; do not widen server-level permissions without confirmation.
- If STT returns empty or hallucinated transcript, do not send a synthetic user event.
- If TTS generation fails, fall back to text response; do not loop retries indefinitely.
- If gateway logs show repeated exceptions or reconnect loops, stop VC testing and run root-cause debugging before further live attempts.
- If the Discord API/library voice internals have changed and current `VoiceReceiver` no longer receives packets, stop and write a spike result before attempting architectural changes.

## Implementation tasks

### Task 1: Add a local voice dependency preflight script

**Objective:** Create a repeatable, no-secret preflight that checks Python voice packages plus system `ffmpeg` and libopus before any live Discord VC test.

**Files:**
- Create: `scripts/check_discord_voice_runtime.py`
- Test: `tests/scripts/test_check_discord_voice_runtime.py`

**Step 1: Write failing tests**

Create `tests/scripts/test_check_discord_voice_runtime.py` with dependency injection so the test does not depend on the host machine:

```python
from scripts.check_discord_voice_runtime import check_runtime


def test_check_runtime_passes_when_all_dependencies_present():
    result = check_runtime(
        find_spec=lambda name: object(),
        which=lambda name: f"/usr/bin/{name}",
        find_library=lambda name: f"lib{name}.dylib",
        exists=lambda path: False,
    )
    assert result["ok"] is True
    assert result["missing"] == []


def test_check_runtime_fails_closed_when_ffmpeg_missing():
    result = check_runtime(
        find_spec=lambda name: object(),
        which=lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}",
        find_library=lambda name: f"lib{name}.dylib",
        exists=lambda path: False,
    )
    assert result["ok"] is False
    assert "ffmpeg" in result["missing"]


def test_check_runtime_accepts_homebrew_opus_fallback():
    result = check_runtime(
        find_spec=lambda name: object(),
        which=lambda name: f"/usr/bin/{name}",
        find_library=lambda name: None if name == "opus" else f"lib{name}.dylib",
        exists=lambda path: path == "/opt/homebrew/lib/libopus.dylib",
    )
    assert result["ok"] is True
    assert "lib:opus" not in result["missing"]
```

**Step 2: Run test to verify failure**

Run:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest tests/scripts/test_check_discord_voice_runtime.py -q -o 'addopts='
```

Expected: FAIL because `scripts/check_discord_voice_runtime.py` does not exist.

**Step 3: Implement minimal script**

Create `scripts/check_discord_voice_runtime.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import ctypes.util
import importlib.util
import json
import os
import shutil
from typing import Callable, Any

REQUIRED_MODULES = ["discord", "nacl", "davey", "edge_tts", "faster_whisper"]
OPTIONAL_MODULES = ["mutagen"]
REQUIRED_BINARIES = ["ffmpeg"]
REQUIRED_LIBRARIES = ["opus"]
MACOS_HOMEBREW_OPUS_PATHS = [
    "/opt/homebrew/lib/libopus.dylib",
    "/usr/local/lib/libopus.dylib",
]


def _has_library(name: str, find_library: Callable[[str], str | None], exists: Callable[[str], bool]) -> bool:
    if find_library(name):
        return True
    if name == "opus":
        return any(exists(path) for path in MACOS_HOMEBREW_OPUS_PATHS)
    return False


def check_runtime(
    *,
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
    which: Callable[[str], str | None] = shutil.which,
    find_library: Callable[[str], str | None] = ctypes.util.find_library,
    exists: Callable[[str], bool] = os.path.exists,
) -> dict:
    missing: list[str] = []
    optional_missing: list[str] = []

    for name in REQUIRED_MODULES:
        if find_spec(name) is None:
            missing.append(f"python:{name}")
    for name in OPTIONAL_MODULES:
        if find_spec(name) is None:
            optional_missing.append(f"python:{name}")
    for name in REQUIRED_BINARIES:
        if which(name) is None:
            missing.append(name)
    for name in REQUIRED_LIBRARIES:
        if not _has_library(name, find_library, exists):
            missing.append(f"lib:{name}")

    return {
        "ok": not missing,
        "missing": missing,
        "optional_missing": optional_missing,
    }


def main() -> int:
    result = check_runtime()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 4: Run test to verify pass**

Run:

```bash
venv/bin/python -m pytest tests/scripts/test_check_discord_voice_runtime.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Run live preflight**

Run:

```bash
venv/bin/python scripts/check_discord_voice_runtime.py
```

Expected before system install: FAIL listing `ffmpeg` and `lib:opus`. Expected after Task 2: PASS or only optional `python:mutagen` missing.

**Step 6: Commit**

```bash
git add scripts/check_discord_voice_runtime.py tests/scripts/test_check_discord_voice_runtime.py
git commit -m "test(discord): add voice runtime preflight"
```

### Task 2: Install and verify system voice dependencies on macOS

**Objective:** Make `ffmpeg` and libopus discoverable to the same environment that runs Hermes gateway.

**Files:**
- Modify: none initially; this is machine setup.
- Evidence: save command output in the implementation report, not in repo unless later requested.

**Step 1: Check package manager**

Run:

```bash
command -v brew
```

Expected: path to Homebrew. If missing, stop and ask before installing Homebrew.

**Step 2: Install dependencies**

Run:

```bash
brew install ffmpeg opus
```

Expected: Homebrew reports installed or already installed.

**Step 3: Verify from Hermes venv**

Run:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python scripts/check_discord_voice_runtime.py
```

Expected: `"ok": true`. Optional `mutagen` may be reported under `optional_missing`; it must not block VC playback.

**Step 4: If libopus is still not discoverable**

Run:

```bash
brew --prefix opus
```

Then verify `/opt/homebrew/lib/libopus.dylib` or `/usr/local/lib/libopus.dylib` exists. `gateway/platforms/discord.py:545-554` already checks both paths on macOS. If neither exists, stop and debug install path rather than changing code.

### Task 3: Add/verify Korean-friendly voice configuration

**Objective:** Configure the use case for Korean commute instructions while preserving existing provider choices and avoiding credentials.

**Files:**
- Modify: `/Users/honbul/.hermes/config.yaml` only if config keys are absent or wrong.
- Evidence: note before/after values in implementation report; do not commit user config unless explicitly desired.

**Step 1: Snapshot current config values**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
import yaml, json
p = Path('/Users/honbul/.hermes/config.yaml')
data = yaml.safe_load(p.read_text())
print(json.dumps({k: data.get(k) for k in ['voice', 'stt', 'tts']}, ensure_ascii=False, indent=2))
PY
```

Expected current facts: STT local enabled, provider local, TTS edge, voice auto_tts false.

**Step 2: Set STT language to Korean for this profile**

If `stt.local.language` is empty, set it to Korean:

```bash
hermes config set stt.local.language ko
```

Expected: config update succeeds. If Korean/English mixed transcription becomes worse in smoke testing, roll back to empty language.

**Step 3: Choose a Korean Edge TTS voice**

Set a Korean voice, for example:

```bash
hermes config set tts.edge.voice ko-KR-SunHiNeural
```

Expected: config update succeeds. If the provider rejects the voice, use `ko-KR-InJoonNeural`.

**Step 4: Keep global `voice.auto_tts` false**

Do not globally set `voice.auto_tts: true`. `/voice channel` sets per-chat voice mode to `all`, which is narrower and easier to roll back.

### Task 4: Restart gateway and verify slash command availability

**Objective:** Ensure the gateway reloads config/code/dependencies and Discord exposes `/voice channel`.

**Files:**
- Modify: none.

**Step 1: Restart gateway**

Use the gateway control appropriate to the local install:

```bash
hermes gateway restart
```

If the command is unavailable or fails, inspect:

```bash
hermes gateway status
```

**Step 2: Check logs for dependency warnings**

Run a tool-discipline-safe Python log scan:

```bash
python3 - <<'PY'
from pathlib import Path
terms = ['opus', 'ffmpeg', 'voice receiver', 'voice channel', 'traceback']
log = Path('/Users/honbul/.hermes/logs/gateway.log')
lines = log.read_text(errors='ignore').splitlines() if log.exists() else []
for line in [l for l in lines if any(t in l.lower() for t in terms)][-80:]:
    print(line)
PY
```

Expected: no fresh `Opus codec not found` warning after restart.

**Step 3: Check Discord slash command**

In the current Discord server text channel or thread, run:

```text
/voice status
```

Expected: bot responds with current voice mode. If slash commands are stale, use text command `/voice status` in a channel where the bot can read messages, then debug command sync separately.

### Task 5: Add exact Discord VC permission-gap reporting

**Objective:** Fail closed before joining a VC when Hermes lacks `Connect` or `Speak`, and report the exact missing permission(s).

**Files:**
- Modify: `gateway/run.py` or `gateway/platforms/discord.py` at the narrow join preflight boundary.
- Test: `tests/gateway/test_voice_command.py`

**Step 1: Write failing test**

Add a test near the existing gateway voice channel command tests:

```python
@pytest.mark.asyncio
async def test_voice_channel_join_reports_missing_connect_permission():
    runner = _make_runner_for_voice_tests()
    event = _make_discord_event(text="/voice channel", chat_id="123", guild_id=111, user_id="42")
    adapter = runner.adapters[Platform.DISCORD]
    adapter.get_user_voice_channel = AsyncMock(return_value=_voice_channel_with_permissions(connect=False, speak=True))

    result = await runner._handle_voice_channel_join(event)

    assert "missing" in result.lower()
    assert "Connect" in result
    assert "Speak" not in result


@pytest.mark.asyncio
async def test_voice_channel_join_reports_missing_speak_permission():
    runner = _make_runner_for_voice_tests()
    event = _make_discord_event(text="/voice channel", chat_id="123", guild_id=111, user_id="42")
    adapter = runner.adapters[Platform.DISCORD]
    adapter.get_user_voice_channel = AsyncMock(return_value=_voice_channel_with_permissions(connect=True, speak=False))

    result = await runner._handle_voice_channel_join(event)

    assert "missing" in result.lower()
    assert "Speak" in result
    assert "Connect" not in result


@pytest.mark.asyncio
async def test_voice_channel_join_reports_both_missing_permissions():
    runner = _make_runner_for_voice_tests()
    event = _make_discord_event(text="/voice channel", chat_id="123", guild_id=111, user_id="42")
    adapter = runner.adapters[Platform.DISCORD]
    adapter.get_user_voice_channel = AsyncMock(return_value=_voice_channel_with_permissions(connect=False, speak=False))

    result = await runner._handle_voice_channel_join(event)

    assert "missing" in result.lower()
    assert "Connect" in result
    assert "Speak" in result
```

Use existing test fixtures/helpers in `tests/gateway/test_voice_command.py`; if helpers differ, adapt names but preserve the assertion: exact missing permission names must be reported for missing `Connect`, missing `Speak`, and both missing together.

**Step 2: Run test to verify failure**

Run:

```bash
venv/bin/python -m pytest tests/gateway/test_voice_command.py -q -o 'addopts='
```

Expected: FAIL until permission-gap logic exists.

**Step 3: Implement minimal permission preflight**

Before `adapter.join_voice_channel(voice_channel)`, compute bot member permissions in the target channel and return a message like:

```text
Cannot join voice channel: missing Discord permission(s): Connect
```

Do not widen permissions automatically. Do not request server-admin privileges.

**Step 4: Run focused tests**

Run:

```bash
venv/bin/python -m pytest tests/gateway/test_voice_command.py -q -o 'addopts='
```

Expected: PASS.


### Task 6: Run focused automated tests

**Objective:** Verify existing voice behavior remains correct before live Discord testing.

**Files:**
- Modify: none unless tests fail for a real regression discovered during this task.

**Step 1: Run focused gateway tests**

Run:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python -m pytest tests/gateway/test_voice_command.py -q -o 'addopts='
```

Expected: PASS.

**Step 2: Run integration audio-flow tests**

Run:

```bash
venv/bin/python -m pytest tests/integration/test_voice_channel_flow.py -q -o 'addopts='
```

Expected: PASS.

**Step 3: If failures occur**

Follow `systematic-debugging`: reproduce exact failing test, inspect traceback, trace to component, patch only root cause, add/update regression test, rerun focused suite.

### Task 7: Live Discord VC smoke test

**Objective:** Prove the actual Discord VC loop works end-to-end in the JÖKL server with the current user.

**Files:**
- Modify: none.
- Evidence: implementation report may record timestamps and sanitized transcript snippets.

**Step 1: Confirm privacy boundary**

Use a private/operator-approved VC and a private/operator-approved linked text channel/thread. Prefer only 상현 plus Hermes in VC. If any other participant is present, require explicit consent in the linked text channel before continuing.

**Step 2: User joins a Discord voice channel**

상현 joins the target VC from mobile or desktop.

**Step 3: Bind the VC from a linked text channel/thread**

In the text channel/thread where audit logs should appear, run:

```text
/voice channel
```

Expected response:

```text
Joined voice channel **<name>**.
I'll speak my replies and listen to you. Use /voice leave to disconnect.
```

**Step 4: Confirm status**

Run:

```text
/voice status
```

Expected: mode `TTS` or equivalent, VC name, participant list.

**Step 5: Speak a short Korean utterance**

Say:

```text
헤르메스, 오늘 출근길 테스트야. 짧게 한 문장으로 대답해줘.
```

Expected in linked text channel:

```text
**[Voice]** <@USER_ID>: 헤르메스, 오늘 출근길 테스트야. 짧게 한 문장으로 대답해줘.
```

Minor transcription differences are acceptable if intent is preserved.

**Step 6: Verify response playback**

Expected: Hermes responds in the VC with a short Korean TTS response. If text response appears but no audio plays, inspect ffmpeg/libopus and `play_in_voice_channel()` logs.

**Step 7: Stop session**

Run:

```text
/voice leave
```

Expected: bot leaves VC and voice mode is off.

### Task 8: Add a concise VC operating runbook

**Objective:** Document the exact operator flow for future 출근길 use without re-reading this plan.

**Files:**
- Create: `docs/runbooks/discord-voice-channel-hermes-runbook.md`

**Step 1: Draft runbook with PKM frontmatter**

Include:

- prerequisites
- privacy/consent boundary: private VC, consented participants only, non-customer-facing linked transcript channel
- raw audio retention rule: temporary buffers only, no raw audio evidence
- start command `/voice channel`
- status command `/voice status`
- stop command `/voice leave`
- recommended speaking style: short, one command per utterance
- fallback: send Discord voice message if live VC fails
- troubleshooting: Opus, ffmpeg, permissions, empty STT, TTS no playback
- rollback: `/voice leave`, restore config voice, gateway restart

**Step 2: Validate frontmatter**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('docs/runbooks/discord-voice-channel-hermes-runbook.md')
text = p.read_text()
assert text.startswith('---\n')
assert '\n---\n' in text[4:]
for marker in ['schema:', 'document_id:', 'title:', '## Start', '## Stop', '## Troubleshooting']:
    assert marker in text, marker
PY
```

**Step 3: Commit**

```bash
git add docs/runbooks/discord-voice-channel-hermes-runbook.md
git commit -m "docs(discord): add voice channel runbook"
```

### Task 9: Final verification, report, and push

**Objective:** Finish with narrow staging, verification, and a clear implementation result.

**Files:**
- Modify: this plan only if execution evidence is appended.
- Create/Modify: implementation report if needed under `docs/reports/` or `.hermes/reports/` with PKM frontmatter.

**Step 1: Run final checks**

Run:

```bash
cd /Users/honbul/.hermes/hermes-agent
venv/bin/python scripts/check_discord_voice_runtime.py
venv/bin/python -m pytest tests/gateway/test_voice_command.py tests/integration/test_voice_channel_flow.py -q -o 'addopts='
git diff --check -- scripts/check_discord_voice_runtime.py tests/scripts/test_check_discord_voice_runtime.py docs/runbooks/discord-voice-channel-hermes-runbook.md
```

Expected: runtime preflight PASS, tests PASS, diff check clean.

**Step 2: Inspect dirty state**

Run:

```bash
git status --short
```

Expected: only task-owned files changed. If unrelated dirty files exist, stage only task-owned files.

**Step 3: Commit and push using helper**

Run with exact paths touched by implementation:

```bash
hermes-finish-work --repo /Users/honbul/.hermes/hermes-agent \
  --message "feat(discord): enable voice channel Hermes workflow" \
  --test "venv/bin/python scripts/check_discord_voice_runtime.py && venv/bin/python -m pytest tests/gateway/test_voice_command.py tests/integration/test_voice_channel_flow.py -q -o 'addopts='" \
  --paths scripts/check_discord_voice_runtime.py tests/scripts/test_check_discord_voice_runtime.py docs/runbooks/discord-voice-channel-hermes-runbook.md docs/plans/2026-05-18-discord-voice-channel-hermes-implementation-plan.md
```

Expected: commit on a user-owned remote (`origin` = `Bichalla/hermes-agent`) and push succeeds without staging unrelated files.

## Rollback plan

1. In Discord, run `/voice leave` in the linked text channel/thread.
2. If gateway is unstable, run `hermes gateway restart`.
3. Restore config values if changed:
   - `hermes config set stt.local.language ''`
   - `hermes config set tts.edge.voice en-US-AriaNeural`
4. If code changes caused regressions, revert only the implementation commits with `git revert <commit>`; do not remove broader existing voice support unless root cause requires it.
5. Verify rollback:
   - `/voice status` shows text-only/off or not connected.
   - gateway logs stop emitting VC errors.
   - focused tests still pass.

## Open questions and non-blockers

- Whether Korean-only STT language (`ko`) is better than auto language detection for mixed Korean/English business terms must be decided by smoke results.
- The exact Discord VC/channel for daily use should be chosen by 상현; implementation can test in any permission-safe server VC.
- Wake-word gating is not part of this MVP. If accidental triggers become a problem, add a later plan for wake-word or push-to-talk semantics.

## Review gate completion instructions

Before implementation, reviewers must answer:

1. Does this plan rely on verified current code/config facts rather than speculation?
2. Are tasks bite-sized and sequential enough for subagent-driven-development?
3. Are tests, smoke checks, rollback, and fail-closed rules sufficient for S3 risk?
4. Are Discord permission and privacy risks bounded?
5. Is `execution_allowed` still false until PASS artifacts exist?

If either required reviewer returns `REQUEST_CHANGES`, patch this document and run focused re-review on the blockers. Both required reviews have passed. The policy block is unlocked only for:

```yaml
execution_allowed: true
execution_scope: dependency_preflight_and_discord_vc_mvp_only
```
