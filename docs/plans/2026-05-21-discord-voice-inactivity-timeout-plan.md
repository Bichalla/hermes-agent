---
schema: pkm-frontmatter/v1
document_id: "discord-voice-inactivity-timeout-plan-20260521"
title: "Discord Voice Inactivity Timeout Implementation Plan"
subtitle: "Hermes Discord VC가 사용자 발화 중에도 300초 타이머로 이탈하는 문제 개선"
created: "2026-05-21T10:02:16+09:00"
updated: "2026-05-21T10:06:31+09:00"
authors:
  - Hermes
owners:
  - honbul
status: review
lifecycle: draft
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
  Hermes Discord voice mode에서 유효한 사용자 음성 입력과 TTS 시작/실패 경로를 활동으로 간주해
  300초 inactivity timeout이 대화 중 VC를 끊지 않도록 하는 TDD 구현 계획이다.
tags:
  - hermes-agent
  - discord
  - voice-mode
  - inactivity-timeout
  - implementation-plan
aliases:
  - Discord VC inactivity timeout fix plan
projects:
  - hermes-agent
areas:
  - agent-ops
  - software-development
resources:
  - /Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py
  - /Users/honbul/.hermes/hermes-agent/tests/gateway/test_discord_race_polish.py
entities:
  people:
    - honbul
  organizations:
    - NousResearch
  brands: []
  products:
    - Hermes Agent
  systems:
    - Hermes Gateway
    - Discord
sources:
  - type: user_request
    title: "Discord VC inactivity timeout 개선 구현 계획 요청"
    url: null
    date: "2026-05-21"
  - type: local_log
    title: "gateway.log voice timeline: VoiceReceiver stopped exactly 300s after previous timeout reset"
    url: /Users/honbul/.hermes/logs/gateway.log
    date: "2026-05-21"
links:
  canonical: /Users/honbul/.hermes/hermes-agent/docs/plans/2026-05-21-discord-voice-inactivity-timeout-plan.md
  source: null
  related:
    - /Users/honbul/.hermes/graphify-pkm/context/related-docs-20260521T010223Z-e2cb0836.md
relations:
  parent: null
  children: []
  depends_on:
    - /Users/honbul/.hermes/docs/hermes-autonomous-recursive-improvement-policy-20260429.md
  supersedes: []
  superseded_by: null
review:
  cadence: on_change
  last_reviewed: "2026-05-21T10:06:31+09:00"
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
version: 0.1.0
---

# Discord Voice Inactivity Timeout Implementation Plan

> **For Hermes:** Use `subagent-driven-development` after this plan reaches review PASS. Implement task-by-task with strict TDD. Do not deploy/restart the live gateway until code review, tests, and a separate restart/verify step pass.

**Goal:** Prevent Hermes Discord voice mode from leaving VC while a valid user utterance or TTS playback cycle is actively being processed.

**Architecture:** Keep the existing per-guild timeout task model. Add narrow timeout refreshes at activity boundaries that already prove real activity: valid non-hallucinated STT transcript, TTS playback start attempt, and playback failure cleanup. Add an explicit timeout-fired log so future diagnosis can distinguish intentional inactivity leave from crash/disconnect.

**Tech Stack:** Python, asyncio, pytest, Discord gateway adapter in `gateway/platforms/discord.py`.

---

## Policy block

```yaml
risk_class: S2
policy_source: /Users/honbul/.hermes/docs/hermes-autonomous-recursive-improvement-policy-20260429.md
required_reviews:
  - plan-engineering-review
  - policy-compliance-review
execution_allowed: true
execution_scope: implement only the allowed_paths code/test fix under subagent-driven-development with no gateway restart or public deploy
measurement_source: pytest focused tests plus runtime gateway log timeline proxy
public_surface_policy: none
forbidden_without_confirmation:
  - public_deploy
  - gateway_restart
  - paid_action
  - credentials
  - DNS
  - irreversible_delete
allowed_paths:
  - /Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py
  - /Users/honbul/.hermes/hermes-agent/tests/gateway/test_discord_race_polish.py
  - /Users/honbul/.hermes/hermes-agent/docs/plans/2026-05-21-discord-voice-inactivity-timeout-plan.md
rollback_strategy: revert the narrow code/test diff; no schema/data migration involved
```

## Verified context inventory

- `/Users/honbul/.hermes/hermes-agent/AGENTS.md`
  - Project uses `.venv` preferred, tests via pytest/scripts, and gateway code under `gateway/platforms/`.
- `/Users/honbul/.hermes/AGENTS.md`
  - S2+ work requires full writing plan, review/update gate, TDD, narrow diffs, verification, and no public deploy without gate.
- `/Users/honbul/.hermes/scripts/hermes_related_docs.py "Hermes Agent Discord voice inactivity timeout implementation plan" --json --limit 8 --neighbors 5 --max-docs 5`
  - Returned quality `ok`; key policy source is `docs/hermes-autonomous-recursive-improvement-policy-20260429.md`.
- `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:575-576`
  - `VOICE_TIMEOUT = 300`.
- `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:1720-1739`
  - `leave_voice_channel()` stops receiver/listen task, disconnects voice client, cancels timeout task, and clears voice state.
- `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:1743-1799`
  - `play_in_voice_channel()` currently resets timeout only after playback/cooldown completes; if playback fails to start it returns `False` before reset.
- `/Users/honbul/.hermes/hermes-agent/gateway/platforms/discord.py:1962-1988`
  - `_process_voice_input()` transcribes audio, rejects failed/empty/hallucinated transcripts, logs valid transcript, and invokes `_voice_input_callback`; it does not currently reset voice timeout on valid user input.
- `/Users/honbul/.hermes/hermes-agent/tests/gateway/test_discord_race_polish.py`
  - Existing async unit tests already construct a minimal `DiscordAdapter` and monkeypatch `_reset_voice_timeout`, FFmpeg audio, and receiver state. This is the best narrow home for regression tests.
- Runtime observation from `gateway.log` investigation in the active session:
  - `VoiceReceiver stopped` occurred exactly 300 seconds after the prior inferred reset, despite a valid user utterance before the stop. This matches a stale inactivity timer, not gateway crash.

## Problem statement

The observed VC auto-leave is consistent with the inactivity timer not being refreshed for user speech. The current implementation refreshes timeout after successful TTS playback completes, but a long response or valid user speech can occur while an older timeout task remains scheduled. If the timeout reaches 300 seconds, it calls `leave_voice_channel()`, stopping the receiver and disconnecting the bot even though the conversation is active.

## Non-goals

- Do not change wake word detection or fuzzy matching in this plan.
- Do not change STT model selection or transcription tooling.
- Do not alter Discord connect/join permissions or channel routing.
- Do not automatically restart the live gateway as part of implementation.
- Do not introduce new long-lived background jobs, cron jobs, or external services.

## Acceptance criteria

1. A valid, non-hallucinated transcript in `_process_voice_input()` refreshes the guild voice inactivity timeout before invoking `_voice_input_callback`.
2. Failed, empty, or hallucinated transcripts do not refresh the timeout.
3. Starting a TTS playback attempt refreshes timeout before waiting for playback completion, preventing a stale timer from firing during a long response.
4. If TTS playback fails to start, the receiver is resumed and the timeout is still refreshed because the voice session was active.
5. Timeout expiry logs an explicit message before leaving the voice channel.
6. Focused pytest tests fail before implementation and pass after implementation.
7. Full touched-test file passes.
8. No public deploy/gateway restart occurs without separate confirmation or policy-gated execution.

## Implementation tasks

### Task 1: Add regression test for valid STT transcript resetting voice timeout

**Objective:** Prove user speech counts as voice activity only after STT returns a useful transcript.

**Files:**
- Modify: `tests/gateway/test_discord_race_polish.py`

**Step 1: Write failing test**

Append tests using the existing `_make_adapter()` helper:

```python
@pytest.mark.asyncio
async def test_process_voice_input_resets_timeout_for_valid_transcript(monkeypatch, tmp_path):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []
    callback_payloads = []
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))

    async def callback(**kwargs):
        callback_payloads.append(kwargs)

    adapter._voice_input_callback = callback

    monkeypatch.setattr("gateway.platforms.discord.tempfile.NamedTemporaryFile", _fake_named_tempfile(tmp_path))
    monkeypatch.setattr("gateway.platforms.discord.VoiceReceiver.pcm_to_wav", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "tools.transcription_tools.transcribe_audio",
        lambda _path: {"success": True, "transcript": "혼불아 안녕"},
    )
    monkeypatch.setattr("tools.voice_mode.is_whisper_hallucination", lambda _text: False)

    await DiscordAdapter._process_voice_input(adapter, guild_id=42, user_id=7, pcm_data=b"pcm")

    assert calls == [("reset_timeout", 42)]
    assert callback_payloads == [{"guild_id": 42, "user_id": 7, "transcript": "혼불아 안녕"}]
```

Also add a tiny helper in the same file:

```python
def _fake_named_tempfile(tmp_path):
    class FakeTmp:
        name = str(tmp_path / "voice.wav")
        def close(self):
            return None
    return lambda *args, **kwargs: FakeTmp()
```

**Step 2: Verify RED**

Run:

```bash
source .venv/bin/activate && pytest tests/gateway/test_discord_race_polish.py::test_process_voice_input_resets_timeout_for_valid_transcript -q
```

Expected: FAIL because `_process_voice_input()` does not call `_reset_voice_timeout()`.

### Task 2: Add negative tests for ignored STT output

**Objective:** Ensure hallucinated/empty/failed transcripts do not keep VC alive.

**Files:**
- Modify: `tests/gateway/test_discord_race_polish.py`

**Step 1: Write failing/passing boundary tests**

Add parametrized tests for failed transcription, empty transcript, and hallucination:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transcription_result,is_hallucination",
    [
        ({"success": False, "transcript": "혼불아"}, False),
        ({"success": True, "transcript": "   "}, False),
        ({"success": True, "transcript": "엔진음,"}, True),
    ],
)
async def test_process_voice_input_does_not_reset_timeout_for_ignored_transcripts(
    monkeypatch, tmp_path, transcription_result, is_hallucination
):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []
    callback_payloads = []
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))

    async def callback(**kwargs):
        callback_payloads.append(kwargs)

    adapter._voice_input_callback = callback

    monkeypatch.setattr("gateway.platforms.discord.tempfile.NamedTemporaryFile", _fake_named_tempfile(tmp_path))
    monkeypatch.setattr("gateway.platforms.discord.VoiceReceiver.pcm_to_wav", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.transcription_tools.transcribe_audio", lambda _path: transcription_result)
    monkeypatch.setattr("tools.voice_mode.is_whisper_hallucination", lambda _text: is_hallucination)

    await DiscordAdapter._process_voice_input(adapter, guild_id=42, user_id=7, pcm_data=b"pcm")

    assert calls == []
    assert callback_payloads == []
```

**Step 2: Verify boundary behavior**

Run:

```bash
source .venv/bin/activate && pytest tests/gateway/test_discord_race_polish.py::test_process_voice_input_does_not_reset_timeout_for_ignored_transcripts -q
```

Expected before production change: PASS if no reset exists; keep this test as guardrail. It must still pass after Task 3.

### Task 3: Implement timeout reset for valid voice input

**Objective:** Refresh timeout after a transcript is proven valid and before dispatching it to the callback.

**Files:**
- Modify: `gateway/platforms/discord.py:1975-1984`

**Step 1: Minimal implementation**

Insert immediately after the valid transcript log or just before the log:

```python
self._reset_voice_timeout(guild_id)
```

Recommended final placement:

```python
if not transcript or is_whisper_hallucination(transcript):
    return

self._reset_voice_timeout(guild_id)
logger.info("Voice input from user %d: %s", user_id, transcript[:100])
```

**Step 2: Verify GREEN**

Run:

```bash
source .venv/bin/activate && pytest tests/gateway/test_discord_race_polish.py::test_process_voice_input_resets_timeout_for_valid_transcript tests/gateway/test_discord_race_polish.py::test_process_voice_input_does_not_reset_timeout_for_ignored_transcripts -q
```

Expected: all pass.

### Task 4: Add TTS timeout refresh tests for playback start and start failure

**Objective:** Prevent stale timeout from firing during long TTS and preserve receiver resume on playback failure.

**Files:**
- Modify: `tests/gateway/test_discord_race_polish.py`

**Step 1: Write failing test for playback-start refresh**

Do not rely on the existing success playback tests for this behavior: current code already resets after playback/cooldown, so a normal immediate-`after` fake can pass without proving early refresh. Add a pending-playback test that holds the `after` callback open and asserts timeout reset happens immediately after `vc.play(...)` starts, before playback completion.

```python
@pytest.mark.asyncio
async def test_play_in_voice_channel_resets_timeout_when_playback_starts(monkeypatch, tmp_path):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    guild_id = 42
    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"fake")
    calls = []
    playback_started = asyncio.Event()
    release_playback = asyncio.Event()
    after_callback = None

    class FakeReceiver:
        def pause(self, clear_buffers=False):
            calls.append(("pause", clear_buffers))

        def resume(self, clear_buffers=False):
            calls.append(("resume", clear_buffers))

    class FakeVoiceClient:
        def is_connected(self):
            return True

        def is_playing(self):
            return False

        def play(self, _source, after=None):
            nonlocal after_callback
            calls.append(("play", None))
            after_callback = after
            playback_started.set()

    async def fake_wait_for(awaitable, timeout):
        await release_playback.wait()
        if after_callback:
            after_callback(None)
        return await awaitable

    adapter._voice_clients[guild_id] = FakeVoiceClient()
    adapter._voice_receivers[guild_id] = FakeReceiver()
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))
    monkeypatch.setenv("HERMES_DISCORD_VOICE_CLEAR_BUFFERS_ON_TTS", "true")
    monkeypatch.setenv("HERMES_DISCORD_VOICE_POST_TTS_COOLDOWN_SECONDS", "0")
    monkeypatch.setattr("gateway.platforms.discord.discord.FFmpegPCMAudio", lambda *_a, **_k: object())
    monkeypatch.setattr("gateway.platforms.discord.discord.PCMVolumeTransformer", lambda source, volume=1.0: source)
    monkeypatch.setattr("gateway.platforms.discord.asyncio.wait_for", fake_wait_for)

    task = asyncio.create_task(
        DiscordAdapter.play_in_voice_channel(adapter, guild_id, str(audio_path))
    )
    await playback_started.wait()
    await asyncio.sleep(0)

    assert calls[:3] == [
        ("pause", True),
        ("play", None),
        ("reset_timeout", guild_id),
    ]

    release_playback.set()
    assert await task is True
```

Expected before production change: FAIL because current code does not call `_reset_voice_timeout()` until after `done.wait()` completes.

**Step 2: Strengthen failure test**

Modify `test_play_in_voice_channel_resumes_receiver_when_play_raises` to expect timeout reset despite `False` return:

```python
assert ("reset_timeout", guild_id) in calls
assert calls[-1] == ("resume", True)
```

Expected before production change: FAIL because the current exception path returns before reset.

### Task 5: Implement TTS start/failure timeout refresh

**Objective:** Refresh timeout as soon as playback activity starts, and refresh in start-failure path before returning.

**Files:**
- Modify: `gateway/platforms/discord.py:1779-1796`

**Step 1: Minimal implementation**

After `vc.play(...)` succeeds, immediately reset timeout:

```python
vc.play(source, after=_after)
playback_started = True
self._reset_voice_timeout(guild_id)
```

In the exception path, reset before returning `False`:

```python
except Exception as e:
    logger.warning("Voice playback failed to start: %s", e, exc_info=True)
    self._reset_voice_timeout(guild_id)
    return False
```

Keep the existing post-playback reset at line 1795. It becomes a final refresh after playback/cooldown. Do not remove it unless a reviewer flags duplicate scheduling as a concrete problem; replacing the timeout task is the intended `_reset_voice_timeout()` behavior.

**Step 2: Verify GREEN**

Run:

```bash
source .venv/bin/activate && pytest tests/gateway/test_discord_race_polish.py::test_play_in_voice_channel_applies_post_tts_cooldown tests/gateway/test_discord_race_polish.py::test_play_in_voice_channel_skips_cooldown_sleep_when_zero tests/gateway/test_discord_race_polish.py::test_play_in_voice_channel_uses_default_cooldown_for_invalid_or_negative_env tests/gateway/test_discord_race_polish.py::test_play_in_voice_channel_resumes_receiver_when_play_raises -q
```

Expected: all pass.

### Task 6: Add explicit inactivity timeout fired log

**Objective:** Make future gateway log diagnosis deterministic.

**Files:**
- Modify: `gateway/platforms/discord.py` near `_reset_voice_timeout()` timeout task body.

**Step 1: Locate current timeout coroutine**

Search:

```bash
rg "def _reset_voice_timeout|VOICE_TIMEOUT|leave_voice_channel" gateway/platforms/discord.py
```

**Step 2: Add log before leave**

Inside the timeout coroutine, immediately before `await self.leave_voice_channel(guild_id)`, add:

```python
logger.info("Voice inactivity timeout fired for guild %s after %ss", guild_id, self.VOICE_TIMEOUT)
```

**Step 3: Add/adjust focused test if structure allows**

If the timeout coroutine is nested and cheaply testable, add a pytest that monkeypatches `asyncio.sleep` and `leave_voice_channel` to assert the log/call. If it requires invasive refactor, skip a new test and rely on code review plus existing timeout behavior; do not refactor solely for logging.

### Task 7: Final verification and review gates

**Objective:** Prove the narrow fix works and is safe to implement later.

**Files:**
- No new files unless review artifacts are explicitly requested.

**Commands:**

```bash
source .venv/bin/activate && pytest tests/gateway/test_discord_race_polish.py -q
source .venv/bin/activate && python -m pytest tests/gateway/test_discord_race_polish.py --tb=short -q
python -m compileall gateway/platforms/discord.py tests/gateway/test_discord_race_polish.py
```

**Independent reviews before implementation is considered complete:**

1. Spec compliance review against this plan.
2. Code quality/security review of the diff.
3. Final integration review if more files are touched than listed in `allowed_paths`.

## Subagent execution packet requirements

If implementation is delegated, each subagent packet must include:

- Plan path: `/Users/honbul/.hermes/hermes-agent/docs/plans/2026-05-21-discord-voice-inactivity-timeout-plan.md`
- `risk_class: S2`
- Review status showing `execution_allowed: true`
- Allowed paths from the policy block
- Forbidden actions: no gateway restart, no public deploy, no credentials, no DNS, no irreversible delete
- TDD requirement: write failing tests first, capture RED/GREEN commands in summary
- Verification commands from Task 7
- Rollback: revert only listed files

## Rollback plan

```bash
git checkout -- gateway/platforms/discord.py tests/gateway/test_discord_race_polish.py
```

If already committed:

```bash
git revert <commit-sha>
```

Rollback must not touch runtime logs, config files, credentials, launchd plist, or Discord state.

## Review evidence

Current review state: PASS. Execution is unlocked only for the narrow code/test scope in the policy block; gateway restart/public deploy remain forbidden without a separate explicit step.

- `plan-engineering-review`: REQUEST_CHANGES on first pass because Task 4's playback-start assertion could pass against existing post-playback reset behavior. Patched Task 4 to use a pending-playback RED test that asserts reset immediately after `vc.play(...)` and before `after` completion. Focused re-review returned PASS.
- `policy-compliance-review`: PASS. Reviewer confirmed S2 risk block, execution lock, forbidden actions, narrow allowed paths, measurement source, rollback, and PKM/frontmatter compliance.
- Final structural validation: PASS. Local validation checked YAML frontmatter keys, required policy/review markers, 7 numbered tasks, `git diff --check`, and narrow git status for this plan file.
