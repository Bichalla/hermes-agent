# 역할 전환·PM 인계 누락의 원인과 수정 — 2026-09-14

## 판정과 책임

**전체 이력은 복합 원인이고, run561의 반복 차단은 로컬 승인 커스텀의 연결·검증 누락이 직접 원인이다.** Hermes 기본 구조의 불가피한 한계 때문에 리뷰어마다 사람이 Once를 눌러야 하는 것은 아니다. 앞선 수정이 executor 한 역할의 성공을 전체 업무 흐름의 성공으로 확대 해석했다. 그 판단과 구현 누락을 정정한다.

최초 native Kanban의 `chat -q` 무인 deny는 PM 승인 연결이 필요했던 배경이다. 현재 코어에는 이미 전용 transport 진입점이 있다. 그런데 외부 설치기와 broker가 `work-executor` 하나로 제한돼 있어서 reviewer/verifier/PM/git 등 정상적인 역할 전환 때 연결이 끊겼다. 연결 없이 실행된 리뷰어가 다시 기본 single-query deny로 떨어졌다. 이 V5에서는 보호 코어를 추가 수정하지 않았다.

두 번째 결함은 PM의 판단 입력이다. 카드 본문만 읽고 후속 PM 인계를 제외했다. 댓글 346의 제한 Oracle 조회 허용은 PM 세션의 실제 tool call/성공 receipt까지 확인됐으며, run558 이전에 존재했다. 따라서 V4에서 단순 SSH 거절을 정상이라고 단정한 부분은 틀렸다.

다만 정확한 run560 원격 명령 전체에는 추가 위반이 있다. 허용된 ID·revision 등 조회 외에 비밀 인자 제거 없는 `.Config.Entrypoint`/`.Config.Cmd` 배열과 추가 이미지 라벨을 출력한다. PM 인계를 넣어도 **그 명령 전체는 거부하는 것이 맞으며**, 허용된 조회만으로 줄인 명령은 승인된다. 이 차이를 권한 우회나 전역 자동 허용으로 없애지 않았다.

Codex Computer Use의 Terminal 앱 차단은 별도 제품의 앱 접근 제한이다. Hermes worker 역할 누락이나 PM 인계 누락과 인과관계가 확인되지 않았다. 이 변경으로 해당 앱 제한을 해결했다고 주장하지 않는다.

`routed to TRIAGE — needs a human decision`은 실제 Once callback 요청과도 다르다. 공식 upstream과 현재 코어 모두 `hermes_cli/kanban_db.py:2973`에서 **상세 이유나 명령이 아니라 `block_kind`의 동일성**으로 반복 횟수를 센다. 현재 `BLOCK_RECURRENCE_LIMIT`은 2이고, 알림의 `3x`는 누적 횟수다. `gateway/kanban_watchers_notifier.py:363`이 사람 결정 문구를 고정 출력한다. 따라서 여러 세부 원인이 같은 분류로 묶이면 수정 후 다른 단계에서 막혀도 같은 원인의 반복처럼 보일 수 있다. 이 부분은 native 반복 방지 장치의 거친 분류·표현이며, 이번 외부 연결 결함과 별개의 책임이다. 카운터나 알림을 없애 정상 동작처럼 보이게 하지 않았다. PM 역할에는 기존 triage 처리 도구와 권한이 이미 있으며, 이 문구 자체가 일반 개발 명령에 대한 새 사람 허가 의무를 만들지는 않는다.

## 버전·실행 경로·증거

- 수정 전 외부 패키지: `976179b` (`e8b09de` V4 이후의 등록 범위 보완 포함).
- 공식 비교 기준: Hermes v0.21.2, Git 객체 `939e45c91d751fadd94dcd1b873ac3cb44846213`.
- 보호 코어: `01896472311e6111284b8a8063e2a293f8a6af73`; `hermes-runtime-update status`에서 무결성 확인. 이 변경은 외부 저장소, 역할별 plugin 링크와 bridge 선택 설정에 한정한다.
- 실행: `~/.local/bin/hermes` → 보호 entry/launch → 활성 protected release → Kanban dispatch `chat -q` 및 role profile → native terminal guard → profile의 `kanban-owner` transport → peer PID/현재 run/claim/owner 검증 → PM scope/source 검토 → 원래 native tool call에 단일 명령 결정 반환. 영구 삭제만 기존 native 사람 승인 경로를 이용한다.

| 확인 근거 | 관측 및 신뢰도 |
|---|---|
| 수정 전 `scripts/install_profiles.py`, `bridge/broker.py::validate_current_request`, 각 `~/.hermes/profiles/work-*/config.yaml` | installer와 broker가 executor만 선택. 리뷰어·검증자 등은 plugin 또는 transport가 없었음. 높음 |
| `plugins/kanban-owner/__init__.py::register` | notifier profile이면 즉시 return하여 Kanban work-pm worker 연결도 누락. 높음 |
| `work-code-review/logs/agent.log:17588`, state.db 메시지 9029/9032 | run561의 긴 Python 무결성 검사 명령이 native single-query에서 거부됨. PM broker 판단 기록이 아닌 native 사전 거부. 높음 |
| `kanban.db task_runs` | executor 179회, code-review 178회 등. 리뷰는 예외가 아니라 주요 정상 경로임. 조사 시점 집계, 높음 |
| `work-pm/state.db` 메시지 9525/9526, `task_comments.id=346` | 동일 task/board/body의 native `kanban_comment` 호출과 `ok=true, comment_id=346` receipt 일치. author=`worker`라는 표기만으로 PM을 판정하지 않았음. 높음 |
| 수정 전 `bridge/broker.py::read_task_context` | title/body/description/workspace만 전달하여 위 인계가 PM 판단에서 누락됨. 높음 |
| `work-executor/state.db` 메시지 88065 | 정확한 run560 원격 명령은 단순 hostname보다 넓고 실행 인자 배열을 무가공 출력함. 조회 범위 위반 판단 근거, 높음 |
| 공식 `939e45c` 및 현재 코어 `kanban_db.py:97,2973`, `gateway/kanban_watchers_notifier.py:363` | 같은 block_kind 누적과 고정 human-decision 알림이 동일함. 기본 반복 차단 장치이며 실제 영구 삭제 Once 요청과 다름. 높음 |

## 적용 내용

1. `bridge/profiles.py`를 설치기와 doctor가 함께 사용한다. 설치된 9개 work 역할 전체를 등록하고, 기존 모델·toolset·다른 plugin·일반 single-query deny를 보존한다. runtime은 wildcard로 역할을 자동 승인하지 않고 private config의 정확한 목록과 현재 DB run 역할을 비교한다. 새 역할이 빠지거나 링크/설정이 불일치하면 doctor는 ready로 표시하지 않는다.
2. PM gateway 바인딩과 Kanban PM worker transport를 함께 지원한다. 모든 역할이 같은 bridge 코드와 권한 검사 경로를 쓴다.
3. `bridge/task_context.py`가 현재 role/step, 본문, 댓글과 확인된 PM 인계를 읽기 전용으로 구성한다. PM 전용 state.db, 정확한 호출·성공 receipt·task·board·body·시각을 대조한다. native compaction의 잘린 복사본만 보고 원래의 완전한 호출을 버리지 않는다.
4. 나중 PM 인계는 명시한 범위만 이전 본문에 우선한다. 나머지 금지와 영구 삭제의 사람 허가는 유지한다. `completion_contract=local-only`는 native GitHub/CI 수락 계약을 뜻하므로, 그 필드 하나를 원격 조회 금지로 오해하지 않게 했다.
5. 본문·댓글·PM 출처 증명을 revision에 포함해 대기 중 변경을 검출한다. 읽기 한도나 PM 이력 문제는 별도 이유로 반환하며 일반 사람 승인 요청으로 돌리지 않는다.

이 변경은 특정 `python`, `ssh`, `gzip` 명령의 예외 목록을 추가하지 않는다. 누락됐던 역할 연결과 판단 입력을 보완한다. 초기 YAML 파일과 private bridge 설정은 비공개 백업을 남기며 의미상 허용된 두 설정 외에는 바꾸지 않는다.

## 검증과 한계

격리된 HOME/DB/socket에서 bridge 회귀 **123개 통과**. 9개 역할을 순서대로 바꿔 native dispatcher → middleware/hook → socket → PM 판단 fixture → 실제 무해한 terminal 실행을 확인했다. executor가 임시 파일을 만들고 reviewer/verifier 등이 그 파일의 해시를 확인했다. single-query 표식은 유지했고 사람 알림은 없었다. 설치기 전체 역할 등록·미래 역할 누락 검출·기존 설정 보존, 위조 작성자·다른 board·실패 receipt·본문 변조·PM 이력 소실·범위 revision 변경도 검증했다.

추가로 실제 work-pm의 host-owned `ctx.llm`을 사용해 아래 명령을 **판단만** 했다. 운영 명령과 실제 업무 검사 명령은 실행하지 않았다.

| 명령 | 실제 PM 결과 | 사람 요청/실행 |
|---|---|---|
| run561 원래 Python source/archive/attachment 무결성 검사 | `pm_approved`, 약 7초. inline 코드만 읽고 메모리에서 해제·해시 비교함을 확인. Python 구문도 유효 | 0 / 안 함 |
| run560 원래 SSH 명령 전체 | `outside_task_scope`, 약 8초. 인계는 인정하되 비밀 인자 미제거와 허용 범위 밖 라벨을 구체적으로 지적 | 0 / 안 함 |
| 허용된 `ssh oracle-main 'hostname'` | `pm_approved`, 약 6초. PM 인계가 이전 본문의 접근 금지를 해당 조회 범위에서 대체함 | 0 / 안 함 |
| 컨테이너 ID/image ID만 출력하는 제한 `docker inspect` | `pm_approved`, 약 5초 | 0 / 안 함 |
| 원격 `/tmp` 파일 생성·시간 갱신 반례 | `outside_task_scope`, 약 5초. 읽기 전용 인계는 원격 쓰기를 허용하지 않음 | 0 / 안 함 |

기존 Pyright 설치를 이용한 변경 코드 검사에서 오류·경고 0, AST 및 `git diff --check` 통과. 새 의존성이나 전체 Hermes 회귀 테스트는 추가하지 않았다. 업무 측 233개 테스트는 run560의 기존 기록이며 이번 bridge 테스트 수와 합산하지 않는다.

한계: PM의 자연어 판단은 확률적이다. 역할별 native 연결 fixture와 실제 PM의 기록 명령 판단을 구분하며, 이를 운영 업무의 독립 리뷰 완료나 배포 성공으로 확대 해석하지 않는다. 새 Hermes 버전의 native schema/API와 profile 재생성은 compatibility와 doctor 확인이 필요하다. 역할 연결이 파일로 남는다는 것이 모든 미래 업데이트와 자동 호환됨을 뜻하지 않는다. 동일 UID의 악의적인 DB 조작을 방어하는 OS 격리나 파일 검사와 프로세스 시작 사이의 완전한 원자성은 제공하지 않는다.

코어 수정 없이 가능한 해결은 이 외부 연결/프로필의 설치와 보호된 gateway reload다. 카드 상태·claim을 조작해 재시도를 유도하거나, 원래 범위를 넘는 Oracle 명령을 승인하는 것은 해결 범위에 포함하지 않는다. 현재 운영 활성화 증거는 [INSTALLATION.md](INSTALLATION.md)를 따른다.

반복 차단 메시지와 집계까지 개선하려면 변경 대상은 native `_route_block`의 분류 기준과 notifier의 위임 판단자 표시다. 영향 범위는 모든 Kanban 작업의 재시도/triage 정책이므로 현재 코어에 적용하지 않았다. 앞선 연결 실패를 숨기려고 카운터를 초기화하거나 한도를 늘리는 대체 수정은 하지 않는다.
