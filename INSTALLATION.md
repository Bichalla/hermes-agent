# Installation status — 2026-09-14

## V2: work-pm 위임 승인

사용자의 명시적 정책에 따라 일반 개발은 work-pm이 판단하고, 전용 도구의 복구 가능한 파일 삭제는 사람 허가 없이 처리한다. 영구 삭제는 사람만 허가할 수 있다.

- 기존 운영 코어: `05e4bf9383bed637498872dbec349e8feb9329d2`
- V2 후보 코어: `01896472311e6111284b8a8063e2a293f8a6af73`
- 공식 기반: `939e45c91d751fadd94dcd1b873ac3cb44846213`, Hermes v0.21.2
- V2 receipt: `/Users/honbul/.hermes/runtime/protected-releases/hermes-candidate-01896472311e-20260914T015226Z-7b2e8585/candidate-receipt.json`
- 후보 준비 및 기존 보호 관리자의 finalize가 통과했다. 운영 전환 결과는 아래 상태를 따른다.

V2는 코어의 `tools/approval.py` 한 파일을 추가 변경하여, 선택된 Kanban worker의 모든 terminal 명령을 위임 정책으로 보낸다. 기존 승인 off/yolo/permanent grant나 container 분기가 이 정책보다 앞서지 않는다. Hermes의 기존 hardline/user deny는 유지한다. V1부터의 전체 연결부는 세 파일이며 `compat/core.patch`는 V1 이전 `e96bfd0`부터의 전체 패치를 보관한다. manifest의 후보 준비 기준은 V1 활성 코어 `05e4bf9`이다.

## 현재 운영 상태

**V2 구현·테스트·Git push는 완료했지만 운영 활성화는 미완료다.** 기존 관리자의 전환을 시도했으나 실행 중 작업이 있어 drain timeout으로 종료됐다. 60초 대기 뒤 300초로 추가 대기했으며, 마지막에 실행 542 (`t_c7ee8515`)와 543 (`t_a68be24d`)가 남아 있었다. 두 프로세스의 생존도 읽기 전용으로 확인했다. 중간 실행 545는 자연 종료됐다.

활성 코어는 여전히 `05e4bf9`이다. V2 프로필 연결은 설치됐지만 V2 manifest는 `0189647`만 허용하므로 현재 `doctor`는 `ready: false`다. 이는 새 정책의 운영 동작을 확인한 상태가 아니며, 승인 호환성 검사를 완화하지 않았다. V1의 기존 준비 상태가 그대로 유지된다고 주장하지 않는다.

기존 runtime-protection 전환 잠금이나 drain을 생략하지 않았고 실행/카드 상태를 강제로 변경하지 않았다. 남은 단계는 작업이 자연 종료된 뒤 이미 준비된 위 V2 receipt를 기존 `hermes-runtime-update activate`로 활성화하고 `doctor`의 runtime/Gateway/reviewer 항목을 다시 확인하는 것이다. 자동 재시도 작업은 등록하지 않았다.

## 설치된 외부 연결

- work-executor의 `plugins/kanban-owner`: worker transport, soft-delete/restore 도구, patch 삭제 훅.
- work-pm의 `plugins/kanban-owner`: 현재 work-pm 모델의 native `ctx.llm` 바인딩.
- work-pm의 `hooks/kanban-owner`: PM 판단과 사람 영구 삭제 승인 경로를 조합한 broker.
- private 설정 `.local/config.json`, socket `.local/run/owner.sock`, worker trash `.local/trash`는 Git에 포함하지 않는다.
- 원본 프로필 설정은 `.local/backups/`에 비공개 백업했다. 기존 플러그인, 모델, 인증 설정은 유지한다.
- 점검 명령: `~/.local/bin/hermes-approval-bridge doctor`. `pm_reviewer_ready`는 바인딩을 확인하며, 실제 모델 응답이나 사람 클릭 자체를 뜻하지 않는다.

## 검증

- 봉인된 실제 V2 후보 source/venv에서 기능 테스트 **89개 통과**.
- 임시 HOME/DB/socket으로 core → plugin → broker → PM 또는 native human fixture 경로 검증.
- 일반 명령의 PM 자동 승인, hard-delete의 모델 승인 불가, opaque 명령 거부, 재할당·경로·작업 내용 변경에 따른 판단 무효화 검증.
- soft-delete 이동/복원, private receipt, 원자적 덮어쓰기 거부, 메타데이터 갱신 실패 후 복구, 경로 탈출·symlink·특수파일·cross-device 거부 검증.
- 실제 work-pm 프로필 플러그인을 native PluginManager로 로드하고 읽기 전용 `git status --short`에 대한 모델 판단만 호출했다. `approve / non_delete / within_task=true` 응답을 확인했다. 해당 명령은 실행하지 않았고 Discord 승인 요청도 보내지 않았다.
- AST 문법 검사와 `git diff --check` 통과. 검토 대상에서 비밀 패턴 및 private 파일의 Git 추적이 발견되지 않았다.
- 독립 검토의 알려진 간접 삭제 분류 문제를 수정하고 재검증했다.

## 적용 범위와 한계

이 연결은 선택된 Kanban worker의 Hermes terminal 승인 경로에 적용된다. 임의 프로그램 내부, 다른 MCP 도구, Codex app-server의 별도 실행 경로를 OS 수준으로 통제한다고 주장하지 않는다. 내용이 불투명한 interpreter/eval 명령은 거부하며 worker가 확인 가능한 작업으로 재구성해야 한다.

전용 soft-delete는 같은 파일시스템의 로컬 workspace 파일·디렉터리를 대상으로 한다. DB 레코드와 원격 서비스의 soft-delete는 구현하지 않았다. 원본 위치에 새 파일이 생기면 복원이 이를 덮어쓰지 않는다. 실제 사람의 Discord Once 왕복은 아직 검증하지 않았다.

투자 앱의 운영 배포, 컨테이너 조작, 기존 카드의 blocked/claim/완료 상태 변경은 이 작업에 포함하지 않았다. 설치만으로 종료된 worker를 재개하지 않는다.

## 업데이트와 Git

외부 구현과 프로필 링크는 코어 교체와 별도로 보관한다. 미래 버전 자동 호환을 보장하지 않으며, 업데이트 후보에 `hermes-approval-bridge check <source>`를 실행하고 API/해시 변경은 manifest와 테스트로 다시 검토해야 한다. 활성 보호 코어나 보호 정책을 직접 수정하지 않는다.

원격 저장소: https://github.com/Bichalla/hermes-agent.git

- 코어: `feat/kanban-owner-approval-core`
- 외부 패키지: `custom/kanban-owner-approval-bridge` (독립 이력; Hermes main 병합 아님)

## V1 운영 이력

V1 `05e4bf9`의 보호된 런타임 활성화와 `ready: true` 점검은 앞선 작업에서 완료했다. V1은 위험 명령마다 사람 Once를 요구했으며, V2는 사용자가 새로 위임한 PM 정책으로 이를 대체한다. V1의 실제 사람 클릭 왕복이나 투자 앱 배포를 검증했다는 뜻은 아니다.
