# Installation status — 2026-09-14

## V5: 전체 work 역할과 확인된 PM 인계

**V5 운영 활성화 완료.** 설치된 9개 work 역할에 plugin `0.2.0`과 Kanban 전용 transport를 연결했다. 기존 보호 관리자의 정상 `restart`가 `activated`를 반환했고 default 및 work-pm Gateway가 전환됐다. 활성 코어는 `01896472311e6111284b8a8063e2a293f8a6af73` 그대로다.

- 운영 doctor: `broker_policy=work-pm-v5`, 9개 역할의 enrollment/plugin/transport 모두 true, 누락 역할 없음. `pm_history_readable`, `pm_reviewer_ready`, `gateway_on_candidate`, 최종 `ready` 모두 true.
- 설치된 각 역할을 별도 프로세스의 native `PluginManager.discover_and_load()`로 검사했다. 9개 모두 plugin `0.2.0`, 오류 없음, approval transport와 execution middleware 등록을 확인했다. 이 등록 검사는 명령을 실행하거나 실 운영 카드에 실행권을 만들지 않는다.
- 격리 bridge 테스트 **123개 통과**. 실제 native terminal을 통한 9개 역할 전환 fixture와 별도의 실제 PM 모델 판단 5건을 구분해 검증했다. run561 원래 무결성 명령은 `pm_approved`; 허용된 제한 Oracle 조회도 `pm_approved`. run560 원래 전체 조회와 원격 쓰기 반례는 구체적 범위 위반으로 거부됐다. 기록된 업무 명령을 실제 실행하지 않았다.
- 설정 백업은 `.local/backups/`의 비공개 파일로 보존했다. 변경은 plugin 등록, Kanban transport 선택, private bridge 역할 목록/PM 이력 경로이며 모델·인증·일반 single-query deny는 유지했다.
- 활성화 전후 세 카드 `t_c7ee8515`, `t_d78243e1`, `t_ed941720`의 status/assignee/current_run_id/worker_pid/claim 해시가 동일했다. 세 카드는 triage이며 running run은 0이었다. 동일 조건 재시도나 새 카드 생성은 하지 않았다.
- `hermes-runtime-update status`: runtime integrity verified, pending transition 없음. 실제 사람 Once 왕복은 검증하지 않았으므로 `human_roundtrip_verified=false`를 유지한다.

V4에서 executor의 성공을 전체 역할 연결의 성공으로 본 부분과 SSH 범위 판단을 정정한다. 원인·책임·현재 한계는 [V5 보고서](APPROVAL-ROOT-CAUSE-V5.md)에 기록했다. 아래 V4 이하 내용은 당시 이력이며 현재 상태는 이 절을 따른다.

## V4: 실행 정보·소스 조사·거절 사유 연결

**V4 운영 활성화 완료.** running 카드가 없음을 확인한 뒤 기존 보호 관리자의 정상 restart가 `status: activated`를 반환했다. default와 work-pm Gateway가 모두 전환됐다.

- 추가 코어 변경 없음. 활성 코어는 `01896472311e6111284b8a8063e2a293f8a6af73` 그대로다.
- 외부 native middleware/hook이 실제 cwd와 call ID를 요청에 연결하고, PM이 제한된 소스 조사 후 판단한다. 검토 파일 변경은 결정을 무효화한다.
- 사람 표시 제한과 PM 전송 한도를 분리했고, terminal 결과에 이유와 구체적인 근거를 반환한다.
- 기능 테스트 **111개 통과**, AST 검사 및 `git diff --check` 통과. 새 의존성 없음. 새 middleware는 Kanban 신원이 있는 worker에서만 등록하여 일반 foreground 세션에 영향을 주지 않는다.
- 실제 native PM + 별도 격리 worker + 훅/소켓으로 원래 네 명령을 **실행 없이** 검토했다. preflight와 로컬 회귀 테스트는 PM 승인, runtime import는 저장 경로 격리 근거 부족, SSH는 카드 범위 위반으로 구분됐다. 사람 요청 0회.
- 별도의 실제 native terminal 실행 fixture에서 임시 파일 생성까지 통과했다. 해당 fixture는 업무 명령을 실행하지 않는다.
- 자세한 원인·관측·한계는 [APPROVAL-ROOT-CAUSE-V4.md](APPROVAL-ROOT-CAUSE-V4.md)에 기록했다. 기존 triage 카드 상태와 claim은 변경하지 않는다.
- 로딩된 broker의 `work-pm-v4 / reviewer_bound=true`를 doctor로 확인했다. `runtime_compatible`, `gateway_on_candidate`, `broker_listening`, `pm_reviewer_ready`, `ready` 모두 true다. 실제 사람 승인은 검증하지 않아 `human_roundtrip_verified=false`를 유지한다.
- V4 본체는 `e8b09de`로 외부 원격 브랜치에 push했다. 마지막 Kanban 등록 범위 보완은 다음 worker의 프로필 plugin 로딩에 적용되며, PM 모델·core·프로필 설정 변경은 없다.

## V3: 일반 판단과 사람 알림의 분리

V2 외부 정책에서 확인한 두 결함을 수정했다. 동일 소유자의 Discord 구독이 여러 개면 일반 PM 판단도 거부하던 결합을 제거했고, heredoc 문법만으로 PM 검토 전에 거부하던 분기를 내용 검토로 변경했다. PM의 추측성 `hard_delete` 분류는 삭제 근거가 없으면 사람에게 보내지 않는다. 근거가 불충분한 명령을 자동 허용하지는 않는다.

V3는 `bridge/broker.py`, `bridge/pm.py`, `bridge/worker.py`와 doctor의 외부 변경이다. 코어는 `01896472311e6111284b8a8063e2a293f8a6af73` 그대로다. 알려진 영구 삭제 명령, 사람 Deny, 실행권·소유자·응답 상관관계 검증은 유지한다. 특정 명령 allowlist를 추가하지 않았다.

- 임시 HOME/DB/socket에서 기능 테스트 **93개 통과**, 변경 Python AST와 `git diff --check` 통과.
- 실제 work-pm native 모델로 보고된 압축·해시·크기 명령과 run555의 정확한 작업공간 확인 명령을 판단만 했다. 둘 다 `pm_approved`, 사람 호출 없음. 해당 명령을 실행하지 않았다.
- 반대 사례인 인라인 Python `os.unlink`는 실제 모델이 `hard_delete / evidence_complete=true`로 분류했다. 실행·사람 승인 요청 없음.
- run555의 현재 다중 구독은 확인했지만 과거 로그가 예외 이유를 숨겨 당시 최초 차단 원인을 단정할 수는 없다. 새 로그는 task/run/request와 이유를 구분한다.
- **V3 운영 활성화 완료.** 사용자의 재요청 후 running 작업이 없음을 확인했고, 기존 보호 관리자의 restart가 `status: activated`를 반환했다. default 및 work-pm Gateway가 모두 정상 전환됐다. 코어 교체, 강제 worker 종료, 카드 상태 변경, drain 생략은 하지 않았다.
- 실행 중인 broker의 읽기 전용 status 응답은 `policy: work-pm-v3`, `reviewer_bound: true`다. doctor의 `runtime_compatible`, `gateway_on_candidate`, `broker_listening`, `pm_reviewer_ready`, `ready`가 모두 true다. 보호 런타임 무결성이 확인됐고 `transition_pending: false`다.
- 외부 코드 수정은 `8eda177`로 원격 `custom/kanban-owner-approval-bridge`에 push했다. 이전 restart(60초) 및 activate(300초)가 `t_ed941720 / run556` 때문에 중단됐던 것은 과거 이력이며 현재 활성화 장애가 아니다.
- 이번 운영 점검은 정책 로딩과 연결 준비 상태 확인이다. 실제 사람의 Discord Once 왕복, 종료된 worker 재개, 투자 앱 배포를 수행했다는 뜻은 아니다.

아래 V2 항목은 설치 기반 및 이전 활성화 이력이다. V3의 판단 정책과 최신 검증 결과는 이 절을 따른다.

## V2: work-pm 위임 승인

사용자의 명시적 정책에 따라 일반 개발은 work-pm이 판단하고, 전용 도구의 복구 가능한 파일 삭제는 사람 허가 없이 처리한다. 영구 삭제는 사람만 허가할 수 있다.

- V2 전환 이전 코어: `05e4bf9383bed637498872dbec349e8feb9329d2`
- V2 활성 코어: `01896472311e6111284b8a8063e2a293f8a6af73`
- 공식 기반: `939e45c91d751fadd94dcd1b873ac3cb44846213`, Hermes v0.21.2
- V2 receipt: `/Users/honbul/.hermes/runtime/protected-releases/hermes-candidate-01896472311e-20260914T015226Z-7b2e8585/candidate-receipt.json`
- 후보 준비 및 기존 보호 관리자의 finalize가 통과했다. 운영 전환 결과는 아래 상태를 따른다.

V2는 코어의 `tools/approval.py` 한 파일을 추가 변경하여, 선택된 Kanban worker의 모든 terminal 명령을 위임 정책으로 보낸다. 기존 승인 off/yolo/permanent grant나 container 분기가 이 정책보다 앞서지 않는다. Hermes의 기존 hardline/user deny는 유지한다. V1부터의 전체 연결부는 세 파일이며 `compat/core.patch`는 V1 이전 `e96bfd0`부터의 전체 패치를 보관한다. manifest의 후보 준비 기준은 V1 활성 코어 `05e4bf9`이다.

## V2 운영 전환 이력

**V2 운영 활성화 완료.** 사용자가 작업 중지를 알린 뒤 다시 확인했을 때 Kanban running 작업은 없었다. work-pm의 활성 대화 1개가 남아 첫 60초 대기는 중단됐으나, 추가 정상 대기 중 해당 대화가 종료되어 기존 보호 관리자가 `status: activated`를 반환했다.

- 활성 코어: `01896472311e6111284b8a8063e2a293f8a6af73`, Git 작업 트리 clean.
- default 및 work-pm Gateway 모두 보호 절차로 새 런타임으로 전환됨.
- 전환 후 doctor: `runtime_compatible`, `gateway_on_candidate`, `broker_listening`, `pm_reviewer_ready`, 최종 `ready` 모두 true.
- 남은 pending transition 없음. 보호 코어 직접 수정, 강제 작업 종료, 카드 상태 변경, drain 생략 없음.
- 실제 사람의 Discord Once 왕복은 검증하지 않았으므로 `human_roundtrip_verified: false`를 유지한다. 이전의 native work-pm 모델 판단 호출 검증과 실제 사람 승인은 구분한다.

이전에 실행 542·543 때문에 전환이 중단됐던 기록은 과거 이력이다. 현재 운영 활성화의 장애가 아니다. 투자 앱의 배포나 기존 worker 재개를 수행한 것은 아니다.

## 설치된 외부 연결

- 설치된 9개 work 역할의 `plugins/kanban-owner`: worker transport, soft-delete/restore 도구, patch 삭제 훅. `scripts/install_profiles.py`가 현재 설치된 work 역할을 공통 경로로 연결한다.
- work-pm의 `plugins/kanban-owner`: Gateway에서는 현재 work-pm 모델의 native `ctx.llm` 바인딩, Kanban worker에서는 transport도 등록한다.
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
