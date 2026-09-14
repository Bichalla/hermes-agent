# 반복 승인 차단의 원인과 V4 교정 — 2026-09-14

## 판정

**전체 이력은 복합 원인이다. 최근 run557~559의 불필요한 반복 차단은 외부 승인 커스텀의 설계·구현 및 검증 누락이 주원인이다.** Hermes 기본 무인 승인 구조만의 불가피한 한계로 돌릴 수 없다. 모든 거절이 오류인 것도 아니다. run558의 SSH는 해당 카드의 운영 접속 금지와 충돌한다.

처음 사람 터미널이 필요했던 것은 기본 Kanban worker가 `chat -q`, 비대화형 표준 입출력으로 실행되고 위험 명령에 대한 무인 deny가 적용되기 때문이다. 이후 로컬 연결은 이 worker를 PM 판단으로 보내도록 바뀌었다. 그러나 V3의 PM에는 스크립트 내용과 실제 cwd를 조사할 수단이 없었고, 사람용 표시 한도가 기계 간 요청에도 적용됐다. 일반 개발을 맡기고도 판단에 필요한 자료를 주지 않은 구조였다.

V3의 93개 테스트와 doctor `ready=true`를 정상 개발 경로가 해결됐다는 근거로 사용한 것은 부족했다. 모의 PM 응답과 연결 준비 상태는 실제 모델이 스크립트를 조사하고 올바른 native 호출에 결정을 반환하는지 입증하지 않는다.

## 버전과 실제 실행 경로

- 공식 비교 기준: Hermes v0.21.2, `939e45c91d751fadd94dcd1b873ac3cb44846213`의 로컬 Git 객체.
- 현재 보호 코어: `01896472311e6111284b8a8063e2a293f8a6af73`. 조사 및 V4 개발 후 Git clean, 보호 무결성 verified.
- V3 외부 구현 기준: `b5bae89` / 기능 변경 `8eda177`.
- 실제 코어 디렉터리: `/Users/honbul/.hermes/runtime/protected-releases/hermes-candidate-01896472311e-20260914T015226Z-7b2e8585/source`.

```text
~/.local/bin/hermes
  → isolated Python → ~/.hermes/ops/runtime-protection/entry.py / launch.py
  → 봉인된 source/.venv (Python 3.11.15)
  → Kanban dispatch: profile + chat -q + task/run/claim/DB 환경
  → work-executor native tool_execution middleware
  → terminal guard: 기존 hardline/user deny → 선택된 Kanban transport
  → native pre_approval_request 훅의 session/turn/tool_call/request ID
  → 외부 worker → private UNIX socket → work-pm broker
  → DB의 현재 실행권·peer PID·소유자·카드 범위 검증
  → work-pm native ctx.llm + 제한된 읽기 전용 소스 조사
  → PM Once/Deny 또는 실제 영구 삭제만 Discord 사람 Once/Deny
  → 요청/소스/실행권 재검증 → 같은 native terminal 호출에 반환
```

`~/.local/bin/hermes:2`는 보호 entry를 실행한다. 보호 `entry.py:33` 이후가 실제 인터프리터와 launch를 선택한다. 현재 코어 `hermes_cli/kanban_db_dispatch.py:2087`, `:2117`, `:2204`, `:2268`에서 worker 명령, 신원 환경, DEVNULL stdin과 로그 stdout을 확인했다. wrapper가 이번 description 제한이나 PM 판단 기준을 넣었다는 증거는 없다.

설치 형태를 구분하면 코어 변경은 `tools/approval.py`, `gateway/run_startup.py`, `gateway/kanban_approval.py`의 연결부다. 외부 저장소의 `compat/core.patch`는 재적용·검토를 위한 보관물이고 실행 중 직접 읽는 패치가 아니다. `compat/manifest.json`은 업데이트 호환성 검사 자료다.

실제 외부 실행물은 이 저장소의 `bridge/`, `plugins/kanban-owner/`, `hooks/kanban-owner/`다. work-executor와 work-pm의 `plugins/kanban-owner` 링크, work-pm의 `hooks/kanban-owner` 링크가 이를 로딩한다. `.local/backups/`와 `.work/`의 과거 파일은 설치 링크와 구분해야 한다. V4는 기존 native middleware/hook을 사용하므로 **새 코어 변경은 없다.** `model_tools.py`, `hermes_cli/middleware.py`, `tools/terminal_tool.py`의 의존 API도 업데이트 hash 계약에 추가했다.

## 원인별 증거와 신뢰도

| 판단 | 근거 | 신뢰도 |
|---|---|---|
| V3는 소스·실행 cwd를 보지 못하고 한 번의 JSON 모델 호출로 판단했다 | `b5bae89:bridge/pm.py`의 `review_with_work_pm`, `bridge/protocol.py`의 요청 필드. 실제 로그의 `needs_evidence`와 일치 | 높음 |
| run557 preflight와 run558 로컬 회귀 테스트는 PM의 근거 부족으로 실행 전에 거절됐다 | work-pm `logs/agent.log:22266`, `:22347`; worker state.db terminal 호출 message 87786, 87928 | 높음 |
| run559 runtime introspection은 당시 PM에 도달하기 전에 잘렸다 | work-executor `logs/agent.log:4121`, request `440f306e6bc74f6fbcc43ffea740fe11`, `description too long`; state.db message 88002 | 높음 |
| run558의 복합 명령도 같은 전송 한도 문제를 겪었다 | work-executor `logs/agent.log:3942`, `:3944`; V3 description 200자 한도 | 높음 |
| run558 SSH 거절은 정상적인 카드 범위 판단이다 | work-pm `logs/agent.log:22341`, request `18d454afc7eb41da876b27a8b95ccc3b`, `outside_task_scope`; 카드 본문의 운영 접속 없는 격리 검증 조건 | 높음 |
| runtime introspection을 무조건 안전한 읽기로 승인해서도 안 된다 | 코어 `model_tools.py:147`, `:160`의 discovery 호출, `tools/registry.py:133`의 캐시 경로, `hermes_cli/plugins.py:1320`의 호환성 보고서 갱신; WorkOS 카드의 첫 import 전 저장 위치 격리 조건 | 높음: 가능한 부수 효과. 당시 실제 쓰기 발생은 미실행이므로 확인 불가 |
| Codex Computer Use의 Terminal 차단은 별개다 | 사용자가 제공한 `com.apple.Terminal` 사용 금지 결과. Hermes PM이나 로컬 wrapper가 이 결정을 유발했다는 증거 없음 | 차단 관측은 사용자 제공 사실; 세부 내부 원인은 미확인 |

로그 기준 디렉터리는 `/Users/honbul/.hermes/profiles/{work-pm,work-executor}`다. 원문 명령은 work-executor의 `state.db`에서 해당 worker 세션의 terminal 호출을 조회했다. 세션은 run557 `20260914_153545_cb5b92`, run558 `20260914_153748_e81e24`, run559 `20260914_153945_98033a`다. 카드의 현재 session 필드를 과거 실행의 증거로 대신 사용하지 않았다.

## 수정한 계약

1. **실행 정보:** `bridge/execution.py`가 native call ID와 host request digest를 연결한다. native terminal 계획에서 실제 cwd를 얻고 동일한 절대 경로로 실행을 고정한다. 명령 문자열만으로 서로 다른 호출을 연결하지 않는다.
2. **근거 조사:** `bridge/evidence.py`와 `bridge/pm.py`가 소스를 import/실행하지 않고 읽는다. 허용된 작업 소스·지원 코어·명령에 직접 참조된 소스 파일만 읽고, private 파일·DB·범위 밖 symlink는 거절한다. 줄 번호, 함수 범위, import 시 실행문을 제공한다. 명령의 실제 호출 경로와 등록만 되는 handler의 미래 동작을 구분한다.
3. **유한한 검토:** 최대 32파일, 파일당 1MiB, 총 512,000자(색인 포함), 6회 조사와 별도의 최종 판단 기회, 기존 전체 승인 deadline을 적용한다. 개발 중 240,000자 예산 소진과 마지막 읽기를 판단하지 않고 끝내는 흐름을 실제 preflight로 발견해 수정했다. 자료 부족을 사람에게 넘기거나 자동 허용하지 않는다.
4. **전송과 사람 표시 분리:** PM 요청은 command 32Ki 문자·description 16Ki 문자, wire 256KiB까지 처리한다. 실제 사람에게 영구 삭제를 요청할 때만 1,200자·완전한 표시 조건을 적용한다. 잘라서 사람에게 승인받지 않는다.
5. **결과 반환:** terminal 결과의 `approval_policy.reason/guidance/details`로 근거 부족, 범위 위반, 사람 거절, 전송·모델 오류를 구분한다. 자유 형식 설명은 강제 redaction하고 길이를 제한한다. 명령·claim·자유 형식 설명을 bridge 로그에 추가하지 않는다.
6. **판단 무효화:** 검토한 파일의 해시·경로를 PM, broker, worker 반환 시 재확인한다. 카드 본문의 표시 한도 뒤에 생긴 변경도 전체 revision으로 검출한다. 기존 재할당·replay·소유자 검증과 hard-delete 사람 허가 경계는 유지한다.

이는 개별 명령 allowlist의 확대가 아니다. 모든 스크립트·인라인 코드에 필요한 실행 정보, 조사 수단, 결과 전달을 제공하는 변경이다. 새 의존성은 추가하지 않았다.

## 최종 검증

격리 HOME/DB/socket의 기능 테스트 **110개 통과**. 실제 native dispatcher/middleware/hook/socket/PM 경로 뒤에 임시 workspace에서 무해한 Python 프로세스가 파일을 생성하는 테스트도 포함한다. 긴 scanner 설명, 서로 다른 call ID, 취소·재할당·카드 변경·소스 변경·잘못된 응답·hard-delete·soft-delete 경계를 확인했다. AST 검사와 `git diff --check` 통과. 전용 Ruff/Mypy/Pyright는 환경에 설치돼 있지 않아 실행하지 않았으며 새 도구를 설치하지 않았다.

별도 검증에서는 실제 work-pm native 모델과 **별도 프로세스의 격리 worker**를 사용했다. 원래 명령·cwd·카드 범위와 실제 scanner 설명을 승인 경로로 전달했다. 해당 명령의 실행 단계는 guard 결과 반환으로 대체했다. 실제 업무 명령을 실행하거나 운영 DB에 테스트 실행권을 만들지 않았다.

| 기록 명령 | 최종 native PM 경로 결과 | 사람 요청 |
|---|---|---|
| run557 `python3 run_activation_preflight.py` | `pm_approved`. synthetic home의 import/등록 경로와 파일 해시 조사를 확인. 약 73초 | 0 |
| run558의 원래 pytest/tee/exit-code 명령 | `pm_approved`. 합성 secret과 임시 경로, main 미호출을 확인. 약 12초 | 0 |
| run559의 원래 보호 runtime import 명령 | `needs_evidence`. discovery가 쓸 cache/report 경로와 활성 plugin 부수 효과가 작업 경계 안임을 확인하지 못한 구체적 설명 반환. 318자 scanner 설명은 전달 성공 | 0 |
| run558 `ssh oracle-main 'hostname'` | `outside_task_scope`. 카드의 운영 접속 금지와 충돌 | 0 |

PM 판단은 확률적이다. 중간 검증에서 import 부수 효과를 놓친 응답을 발견했고, 이를 성공으로 채택하지 않고 import 진입문을 기본 자료에 넣었다. 최종 결과는 현재 파일·카드에 대한 관측이며 모든 미래 코드의 안전성이나 영구적인 동일 모델 응답을 증명하지 않는다. 의도적인 source mutation fixture 외에 기존 업무 파일은 변경하지 않았다.

## 최소 운영 해결책과 남은 책임

V4 외부 패키지를 기존 보호 관리자의 drain/restart로 로딩하면 된다. 코어·프로필·모델·allowlist·보호 정책의 추가 수정은 필요 없다. 최신 운영 활성화 기록은 `INSTALLATION.md`의 V4 절을 따른다.

이후 worker는 반환된 구체적 근거를 통해 자신의 카드가 허용하는 실행을 구성해야 한다. WorkOS의 현재 명령은 import 전에 운영 데이터·캐시 경로를 격리해야 한다는 원래 카드 조건을 충족하지 못했다. 해당 작업의 담당 구현이 이를 충족시키는 것이 필요하며, 승인 서비스가 이를 무시하는 것이 해결책은 아니다. 투자 카드의 SSH도 현재 범위로 자동 승인할 대상이 아니다. 이미 종료된 triage 카드를 자동 재개하거나 다른 카드로 바꾸지 않았다.

커스텀을 전부 제거한 동일 버전에서는 기본 `chat -q` 무인 정책이 남으므로 위험 명령이 자동으로 PM 판단을 받지는 않는다. 이것은 최초 연결이 필요했던 이유다. V3의 소스 조사 결여·표시 한도 전용 실패·결과 전달 누락은 외부 커스텀 책임이며, 코어 기본 구조의 필연적 현상이 아니다. Codex Terminal 앱 차단을 Hermes 수정으로 해결했다고 주장하지 않는다.

실제 사람의 Discord Once 왕복, 세 업무 카드의 실제 시험 실행·WorkOS 구현 완료·투자 앱 운영 배포는 이번 검증에 포함되지 않는다. 소스 해시 검사와 OS 프로세스 시작은 원자적 트랜잭션이 아니며, 이 플러그인은 임의 코드의 모든 부수 효과를 통제하는 OS sandbox가 아니다. 이런 한계가 필요해지면 변경 대상은 terminal 실행 격리 계층과 작업별 권한 계약이며, 명령별 allowlist 추가로 대체할 수 없다.
