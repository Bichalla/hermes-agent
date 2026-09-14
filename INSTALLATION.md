# Installation status — 2026-09-14

구현과 외부 프로필 연결은 설치했지만 **운영 런타임 활성화는 아직 완료되지 않았다**.

## 준비된 릴리스

- 기존 활성 코어: `e96bfd0452f951ae3fe354108840cfee299bb04e`
- 후보 코어: `05e4bf9383bed637498872dbec349e8feb9329d2`
- 공식 기반: `939e45c91d751fadd94dcd1b873ac3cb44846213`, Hermes v0.21.2
- 후보 receipt: `/Users/honbul/.hermes/runtime/protected-releases/hermes-candidate-05e4bf9383be-20260914T003309Z-c2cb83b3/candidate-receipt.json`
- 후보 상태: 기존 manager의 `finalize` 통과, `ready`.
- 코어 변경은 `tools/approval.py`, `gateway/run_startup.py`, `gateway/kanban_approval.py`의 세 파일이다. 활성 보호 코어를 직접 수정하거나 보호를 해제하지 않았다.

## 설치된 외부 연결

- `work-executor/plugins/kanban-owner` → 이 저장소의 worker 플러그인
- `work-pm/hooks/kanban-owner` → 이 저장소의 Gateway 시작 훅
- worker에 `plugins.enabled += kanban-owner`, `security.approval.kanban_transport: kanban-owner` 추가
- private 설정: `.local/config.json`; 소켓: `.local/run/owner.sock`
- 점검 명령: `~/.local/bin/hermes-approval-bridge doctor`
- 원본 worker 설정은 `.local/backups/`에 비공개 백업했다. 기존 플러그인 및 대화형 승인 transport는 유지했다.

## 확인한 검증

- 외부 기능 테스트 54개 통과. 동일 테스트를 봉인된 실제 후보 런타임으로도 실행해 54개 통과했다.
- 임시 HOME/DB/socket으로 core guard → plugin transport → broker → native 승인 대기열 연결, Once/Deny, 재할당 거부를 검증했다.
- worker 최종 응답 직전에도 최초 경로와 현재 경로가 같은지 확인한다. broker 응답 이후 경로 변경도 거부하는 테스트를 포함한다.
- 실제 Hermes PluginManager와 HookRegistry가 임시 프로필의 외부 심볼릭 링크를 탐색·로드했다. 승인 요청은 보내지 않았다.
- 기존 sandbox 정책 아래 외부 `.local`의 임시 소켓 bind가 성공했다. 보호 정책은 수정하지 않았다.
- 후보 의존성 검사, core import 검사, 무결성 검사, 기존 migration 검사 및 외부 호환성 검사가 통과했다.
- 실제 Discord 전송이나 사람의 Once 클릭은 검증하지 않았다. 자동 테스트의 응답은 fixture이며 사람 승인이 아니다.

## 활성화가 남은 이유

기존 `hermes-runtime-update activate <receipt> --timeout 60`을 실행했으나, 다른 카드 `t_9b291278`의 실행 537이 여전히 running이어서 drain timeout으로 안전 중단됐다. 해당 worker PID 59595가 살아 있음을 읽기 전용으로 확인했다.

이 결과는 새 승인 연결의 테스트 실패가 아니라, 실행 중인 작업을 중단하지 않고 코어를 전환하는 기존 보호 절차의 차단이다. 강제 종료, claim 회수, 카드 상태 변경 또는 drain 생략은 하지 않았다. 활성 코어가 바뀌지 않아 현재 `doctor`의 `ready`는 false이며 broker도 아직 시작되지 않았다.

후속 전환은 실행 중인 작업이 정상 종료된 뒤 동일 receipt에 기존 보호 관리자의 activate 절차를 다시 적용하는 것이다. 이후 doctor의 runtime/gateway/socket 검증과 실제 소유자 승인 왕복을 별도로 확인해야 한다. 이미 종료된 투자 앱 worker는 이 설치만으로 재개되지 않는다.

투자 앱 배포, 컨테이너 조작, 기존 투자 카드의 claim/blocked/완료 상태 변경은 수행하지 않았다.

## 업데이트 보존의 범위

외부 구현, 프로필 링크, 정확한 core patch와 호환성 manifest를 분리 보관했다. 런타임에 연결부가 없거나 지원되지 않는 버전이면 플러그인과 훅은 승인 기능을 활성화하지 않는다. 현재 보호 관리자를 수정해 검사 절차를 우회하지 않았다.

이는 미래 버전과의 무조건 자동 호환 보장이 아니다. 업데이트 후보에는 `hermes-approval-bridge check <source>`를 먼저 수행하고, 새 버전에서는 연결부/API 변경을 검토한 뒤 manifest와 테스트를 갱신해야 한다.
