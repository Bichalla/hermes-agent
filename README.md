# Hermes Kanban owner approval bridge

자동 Kanban worker가 위험 명령 앞에서 기존 Discord 소유자 승인 버튼을 기다리도록 연결한다. 사람이 **Allow Once**를 누르면 같은 worker의 해당 요청에만 승인 결과를 돌려준다. 카드에 작업을 승인했다는 사실은 명령 실행 승인으로 간주하지 않는다.

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

native UI는 Once와 Deny만 제공한다. 세션/영구 승인은 저장하지 않는다. 요청 ID와 digest로 응답을 묶고, 타임아웃·연결 종료·재할당·Gateway 종료·전송 실패·권한 불일치·재사용 응답은 거부한다. 대기 정보는 메모리에만 보관하며 재시작 시 사라진다. 소켓에는 승인 결정을 주입하는 API가 없다.

표시할 수 없는 긴 명령이나 Markdown 코드 블록을 깨뜨리는 명령은 잘라서 승인받지 않고 거부한다. 명령은 기존 Hermes redactor를 거쳐 전달한다. 원본 명령은 host digest에 묶이며 외부 중계 로그에 저장하지 않는다.

## 업데이트

외부 저장소와 프로필 링크는 코어 교체와 별개로 남는다. 하지만 이것이 모든 미래 Hermes 버전과 자동 호환됨을 뜻하지는 않는다. 후보 코어는 지원 버전, native API, 연결부 hash를 먼저 검사해야 한다. 호환되지 않는 후보에는 새 계약과 격리 테스트가 필요하다. 연결부를 삭제한 코어에서 승인을 자동 허용하는 fallback은 없다.

기존 runtime-protection의 무결성 검사, migration 검사, drain·전환·복구 절차를 그대로 사용한다. 이 저장소의 준비 도구는 활성 코어가 아닌 깨끗한 별도 Git 후보를 대상으로 한다. upstream의 다른 변경이나 패키지 의존성은 이 기능을 위해 추가하지 않는다.

## 검증의 한계

자동 테스트는 임시 HOME/DB/socket과 UI fixture를 사용한다. 실제 Discord 사용자 클릭, 실제 투자 앱 배포, 컨테이너 작업을 수행하지 않는다. 테스트의 Once 응답은 테스트 내부 데이터이며 실제 사람 승인의 증거가 아니다.

명령 승인 반환 직전까지 실행 소유권을 재검증하지만 DB 검증과 OS 명령 시작을 하나의 트랜잭션으로 만들지는 않는다. 같은 OS 사용자 권한으로 임의 코드를 실행하는 악성 관리자나 플러그인에 대한 격리 장치도 아니다. 이 기능은 기존 로컬 확장 신뢰 경계 안에서 승인 경로를 연결한다.

투자 앱의 기존 blocked 카드를 자동 재시도하거나 claim/상태를 변경하지 않는다. 새 연결을 설치해도 이미 종료된 worker가 저절로 살아나지는 않는다.

## 설치와 점검

설치 링크는 `~/.hermes/profiles/work-executor/plugins/kanban-owner`와 `~/.hermes/profiles/work-pm/hooks/kanban-owner`다. 기존 플러그인을 유지하고 worker 프로필에 `kanban-owner`와 전용 승인 설정만 추가한다. 원본 프로필 백업은 `.local/backups/`에 비공개로 저장한다.

`hermes-approval-bridge doctor`는 활성 코어 호환성, 프로필 링크, Gateway 커밋, private socket 응답 가능 여부를 읽기 전용으로 확인한다. 승인을 요청하거나 사람의 클릭을 대신하지 않는다. `ready: true`는 연결 준비 상태이고 실제 사람 승인 완료를 뜻하지 않는다.

업데이트 후보에는 먼저 `hermes-approval-bridge check /절대경로/후보/source`를 실행한다. 지원하지 않는 버전은 중단하고 외부 `compat/core.patch`의 연결부를 새 버전에 검토해 적용한 뒤, manifest와 격리 테스트를 갱신한다. 현재 보호 런타임 관리자를 바꾸거나 호환성 검사를 생략하지 않는다.

후보 준비는 `python3 scripts/prepare_candidate.py`로 검사하고 `--prepare`로 비활성 릴리스를 만든다. 그 후 기존 보호 관리자의 finalize와 activate 절차를 사용한다. 실행 중인 작업 때문에 drain이 끝나지 않으면 활성 코어를 교체하지 않는다.
