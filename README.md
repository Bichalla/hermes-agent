# Hermes Kanban owner approval bridge

Kanban worker의 일반 개발 명령은 **work-pm 모델이 작업 범위를 판단해 승인**한다. 사용자는 개발 과정 전체에서 Allow Once를 반복할 필요가 없다. 소유자가 위임한 정책과 실제 사람의 영구 삭제 허가는 서로 다른 권한으로 기록한다.

- 일반 개발: work-pm의 현재 프로필 모델이 `non_delete`, 작업 범위 내, 승인으로 판단하면 해당 명령만 허용한다.
- 복구 가능한 파일 삭제: `kanban_soft_delete`가 현재 카드의 로컬 workspace에서 private trash로 이동하고 복원 receipt를 반환한다. 사람 허가는 필요 없다. `kanban_restore`는 원래 위치가 비어 있을 때만 복원한다. purge 기능은 없다.
- 영구 삭제: 알려진 삭제 명령은 모델 판단 전에 분리하며, 모델이 추가로 발견한 영구 삭제도 소유자의 native Discord Once/Deny로 보낸다. 모델은 사람 허가를 대신할 수 없다.
- 명령 형식만으로 차단하지 않는다. 인라인 코드도 work-pm이 실제 내용을 검토하고, 판단 근거가 충분한 작업 범위 내 비삭제 명령을 승인한다. 동적·인코딩된 내용 등 실행 효과를 확인할 수 없으면 거부한다. 근거 부족을 사람 승인 요청으로 전환하지 않는다.

## V4: 실행 경로와 소스 근거를 갖춘 PM 판단

PM은 native 호출 ID에 연결된 실제 cwd, 카드 범위, scanner 설명을 받고 필요한 소스 파일을 읽은 뒤 판단한다. 읽기는 코드를 import하거나 실행하지 않는다. 함수 범위와 import 시 실행문을 제공하며, 검토한 파일이 바뀌면 결정을 무효화한다. 소스 범위·파일 수·총 크기·조사 횟수·시간에 한도가 있다.

terminal 결과의 `approval_policy.reason/guidance/details`가 근거 부족, 카드 범위 위반, 사람 거절, 인프라 실패를 구분한다. source/handler 등록을 실제 handler 실행과 혼동하거나, 시험의 성공을 시험 실행 승인의 전제 조건으로 요구하지 않는다. PM 판단을 위한 긴 요청과 영구 삭제의 사람 표시 조건도 분리했다.

110개 격리 테스트와 실제 work-pm 모델을 포함한 별도 프로세스의 native 승인 경로로 검증했다. [원인·근거·검증 범위](APPROVAL-ROOT-CAUSE-V4.md), [현재 설치 상태](INSTALLATION.md)를 참조한다. 실제 업무 명령과 사람 클릭까지 검증했다는 뜻은 아니다.

## V3에서 유지한 권한 경계

일반 PM 판단에는 현재 작업 실행권과 고정 소유자를 검증한다. 같은 소유자의 알림 채널이 여러 개라는 이유로 일반 명령을 차단하지 않는다. 유일한 알림 목적지는 실제 영구 삭제의 사람 허가를 보낼 때만 필요하다. 목적지가 모호하면 영구 삭제는 계속 거부한다.

`heredoc` 같은 문법은 검토가 필요하다는 표시이며 자동 거부 사유가 아니다. PM의 `hard_delete` 라벨만으로 사람을 호출하지 않고, 명령 안의 삭제 연산과 충분한 근거를 요구한다. 일반 승인도 `evidence_complete=true`가 필요하다. 기존의 알려진 영구 삭제 분류와 사람 Deny는 PM이 덮어쓸 수 없다. 특정 명령 allowlist를 추가하지 않았다.

task/run/request와 고정된 판단 이유를 로그에 남겨 실행권 실패, 근거 부족, PM 판단, 사람 거부를 구분한다. 원문 명령, claim, 모델의 자유 형식 설명은 로그에 남기지 않는다. V3는 외부 패키지 변경이며 코어 연결부는 V2와 같다.

## 유지보수 경계

- `bridge/`, `plugins/`, `hooks/`: Hermes 코어 밖에서 유지하는 구현.
- `compat/`: 지원하는 코어와 연결부의 버전·무결성 계약 및 재적용 패치.
- `scripts/`: 격리 테스트와 보호된 후보 런타임 준비 도구.
- `.local/`: 이 Mac의 경로·소유자 설정. Git에 포함하지 않는다.
- `.work/`: 실행 중인 코어와 분리된 작업 복사본. Git에 포함하지 않는다.

코어에는 세 파일의 작은 연결부가 필요하다. `tools/approval.py`는 Kanban 전용 승인 transport 진입점, `gateway/run_startup.py`는 시작 훅 전달, `gateway/kanban_approval.py`는 기존 승인 대기열과 Discord UI를 제한된 API로 연결한다. 보호된 실행 코어를 직접 덮어쓰지 않는다.

## 승인 범위

소켓은 외부 저장소의 `.local/run/owner.sock`에 둔다. 기존 보호 정책이 쓰기를 금지하는 `~/.hermes/ops`에 소켓을 만들지 않으며, 보호 정책 자체는 변경하지 않는다.

worker 프로필의 `security.approval.kanban_transport`가 별도로 연결을 선택한다. 기존 대화형 CLI용 `security.approval.transport`는 변경하지 않는다. 일반 `chat -q`, cron, webhook, API 서버에 대한 기존 무인 정책도 유지한다.

broker는 실제 UNIX socket peer PID와 현재 Kanban DB의 task/run/claim/PID/프로필을 대조한다. DB는 쓰기 없이 새 `mode=ro` 연결로 읽는다. 구독 알림 목적지만으로 승인 권한을 부여하지 않으며, 별도로 고정한 소유자와 구독의 소유자·Gateway 프로필이 일치해야 한다. 첫 구현은 Discord만 지원한다.

영구 삭제용 native UI는 Once와 Deny만 제공한다. 세션/영구 승인은 저장하지 않는다. 요청 ID와 digest로 응답을 묶고, 타임아웃·연결 종료·재할당·Gateway 종료·전송 실패·권한 불일치·재사용 응답은 거부한다. work-pm은 host-owned `ctx.llm.complete_structured`로 호출하며 인증정보를 플러그인에 전달하지 않는다. 모델/provider를 하드코딩하지 않고 work-pm의 현재 기본 설정을 따른다. 카드 내용이 대기 중 바뀌어도 판단을 무효화한다. 대기 정보는 메모리에만 보관하며 재시작 시 사라진다. 소켓에는 승인 결정을 주입하는 API가 없다.

영구 삭제의 사람 승인 화면에 표시할 수 없는 긴 명령이나 Markdown 코드 블록을 깨뜨리는 명령은 잘라서 승인받지 않고 거부한다. PM의 기계 간 검토 요청에는 별도의 넓은 전송 한도를 적용한다. 명령은 기존 Hermes redactor를 거쳐 전달한다. 원본 명령은 host digest에 묶이며 외부 중계 로그에 저장하지 않는다.

## 업데이트

외부 저장소와 프로필 링크는 코어 교체와 별개로 남는다. 하지만 이것이 모든 미래 Hermes 버전과 자동 호환됨을 뜻하지는 않는다. 후보 코어는 지원 버전, native API, 연결부 hash를 먼저 검사해야 한다. 호환되지 않는 후보에는 새 계약과 격리 테스트가 필요하다. 연결부를 삭제한 코어에서 승인을 자동 허용하는 fallback은 없다.

기존 runtime-protection의 무결성 검사, migration 검사, drain·전환·복구 절차를 그대로 사용한다. 이 저장소의 준비 도구는 활성 코어가 아닌 깨끗한 별도 Git 후보를 대상으로 한다. upstream의 다른 변경이나 패키지 의존성은 이 기능을 위해 추가하지 않는다.

## 검증의 한계

이 정책의 적용 범위는 선택된 Kanban worker의 Hermes terminal 승인 경로와 native patch 삭제 훅이다. Codex app-server의 별도 실행 경로, 임의 MCP 도구, 악성 플러그인, 임의 프로그램 내부의 모든 파일 변경까지 OS 수준으로 통제하는 장치는 아니다. 문자열 분류와 모델 판단만으로 모든 간접 삭제를 증명할 수 없으므로, 확인 불가능한 명령은 거부한다.

전용 soft-delete는 같은 파일시스템의 로컬 일반 파일·디렉터리만 지원한다. symlink, 특수 파일, `.git`, workspace 루트, trash와 겹치는 경로는 거부한다. cross-device copy/delete 대체 동작은 없다. DB 레코드의 soft-delete나 원격 서비스의 보존 정책은 구현하지 않았다.

자동 테스트는 임시 HOME/DB/socket과 UI fixture를 사용한다. 실제 Discord 사용자 클릭, 실제 투자 앱 배포, 컨테이너 작업을 수행하지 않는다. 테스트의 Once 응답은 테스트 내부 데이터이며 실제 사람 승인의 증거가 아니다.

명령 승인 반환 직전까지 실행 소유권을 재검증하지만 DB 검증과 OS 명령 시작을 하나의 트랜잭션으로 만들지는 않는다. 같은 OS 사용자 권한으로 임의 코드를 실행하는 악성 관리자나 플러그인에 대한 격리 장치도 아니다. 이 기능은 기존 로컬 확장 신뢰 경계 안에서 승인 경로를 연결한다.

투자 앱의 기존 blocked 카드를 자동 재시도하거나 claim/상태를 변경하지 않는다. 새 연결을 설치해도 이미 종료된 worker가 저절로 살아나지는 않는다.

## 설치와 점검

설치 링크는 `~/.hermes/profiles/work-executor/plugins/kanban-owner`, `~/.hermes/profiles/work-pm/plugins/kanban-owner`, `~/.hermes/profiles/work-pm/hooks/kanban-owner`다. 기존 플러그인을 유지하고 worker와 work-pm에 `kanban-owner`를 추가하고 worker에 전용 승인 설정을 둔다. 원본 프로필 백업은 `.local/backups/`에 비공개로 저장한다.

`hermes-approval-bridge doctor`는 활성 코어 호환성, 프로필 링크, Gateway 커밋, private socket 응답과 work-pm reviewer 바인딩을 읽기 전용으로 확인한다. 승인을 요청하거나 사람의 클릭을 대신하지 않는다. `ready: true`는 연결 준비 상태이고 실제 사람 승인 완료를 뜻하지 않는다.

업데이트 후보에는 먼저 `hermes-approval-bridge check /절대경로/후보/source`를 실행한다. 지원하지 않는 버전은 중단하고 외부 `compat/core.patch`의 연결부를 새 버전에 검토해 적용한 뒤, manifest와 격리 테스트를 갱신한다. 현재 보호 런타임 관리자를 바꾸거나 호환성 검사를 생략하지 않는다.

후보 준비는 `python3 scripts/prepare_candidate.py`로 검사하고 `--prepare`로 비활성 릴리스를 만든다. 그 후 기존 보호 관리자의 finalize와 activate 절차를 사용한다. 실행 중인 작업 때문에 drain이 끝나지 않으면 활성 코어를 교체하지 않는다.
