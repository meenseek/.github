# 작업 정리의 실행과 확인

이 도구는 기존 소유자의 승인·판단을 바탕으로 정리의 현재 상태를 읽기 전용으로 확인한다.
작업 실행, Git 변경, 배포, 파일 삭제는 해당 소유자의 기존 도구가 수행한다.
저장소 변경 작업과 `정리` 요청의 범위·완료 조건은 현재 사용자 지시와 적용되는 원래 규칙이 소유한다.
이 문서는 private native 정책의 복사본이나 새 실행 권한이 아니다.

## 실행 경계

[check_completion.py](../scripts/check_completion.py)는 모델·상주 서버 없이 실행한다.
[operations](../../operations/README.md)의 모든 새 `finish --status completed` 호출은
고정한 범위와 실제 검사 결과가 없으면 거부된다. 직접 함수 호출에도 같은 검사가 적용된다.
`blocked`, `cancelled` 전이와 기존 완료 이력은 소급 변경하지 않는다. 고정 범위가 없는
이전 요청은 완료 근거를 조작하거나 원래 요청을 바꿔 통과시키지 않는다. 기존 중단·복구
절차로 남은 작업과 필요한 새 승인을 판단한다.

[completion_hook.py](../scripts/completion_hook.py)는 Codex 채팅의 얇은 어댑터다.
`SessionStart`와 `UserPromptSubmit`에서 준비를 안내하고, `Stop`에서 연결된 정리 작업을
검사한다. 처음 실패하면 continuation을 요청한다. 이미 한 번 이어진 턴에서는 무한 반복을
만들지 않고 미완료를 알린다. 원래 승인된 작업을 계속하거나 구체적인 중단 근거를 남긴다.

**큐 전이는 강제 검사이고 채팅 훅은 보조 장치다.** 미연결 세션, 호출 누락, 호스트 오류,
신뢰 검토 미통과, 다른 훅의 `continue:false`는 이 도구가 강제할 수 없다. transcript나
모델의 마지막 문장에서 의도를 추측하지 않는다. 일반 채팅 전체의 누락 방지가 필요하면
호스트의 최종 완료 전이를 이 검사에 연결해야 한다. 현재 Codex 훅만으로 보장하지 않는다.

## 범위를 먼저 고정하기

저장소 변경 작업은 별도 `정리` 요청이 없어도 이 계약에 연결한다. 명시적인 정리 요청도
같은 절차를 따른다. 현재 `session_id`와 작업 ID로 첫 변경·작업 branch/worktree 생성 전에
`arm`을 실행한다. 읽기 전용 조사·리뷰와 단순 질문만 하는 작업에는 새 변경 범위를 만들지 않는다.
반환되는 pointer 경로와 SHA를 현재 작업 기록에 참조한다. 세션 ID는 훅의 추가 문맥 또는
현재 실행 호스트의 식별자를 사용하며 임의의 다른 세션을 지정하지 않는다.

완료 순서는 변경에 필요한 검증·독립 리뷰, 승인된 main·원격 반영, 필요한 실제 사용본 갱신,
이번 작업의 branch/worktree·임시 자원 정리, 최종 현재 상태 검사로 잇는다. 충돌·검증 실패나
승인 제한으로 남은 단계는 변경을 보존하고 미완료 근거로 남긴다. 다른 작업의 자원은 건드리지 않는다.

훅은 Desktop 안의 경로와 Git common-dir가 Desktop인 외부 worktree에도 같은 안내를
전달한다. 위치 확인은 해당 경로의 읽기 전용 Git 조회만 사용하며, 연결된 작업이 없는
채팅을 자동으로 arm하거나 임의로 병합·삭제하지 않는다.

```sh
python3 scripts/completion_hook.py arm --session SESSION_ID --task-id TASK_ID
python3 scripts/check_completion.py prepare --seed /absolute/seed.json --output /absolute/scope.json
```

구체적인 CLI 입력은 각 명령의 `--help`와 [검증 계약](../scripts/check_completion.py)을
따른다. JSON에는 실행할 shell 명령을 넣지 않는다. seed는 다음을 묶는다.

- 원래 사용자 요청·승인 근거의 경로와 SHA, 작업·세션 식별자, 현재 소유 계약의 경로와 SHA.
- 모든 완료 범주와 고정된 obligation ID. 적용하지 않는 범주는 이유와 고정된 소유 계약 근거.
- 대상 저장소, 필요한 제한된 산출물 경로, `arm`이 반환한 state 경로와 실행 workspace.
- 큐에서는 `ops.py request-digest --request /absolute/request.json`의 정규화한 요청 SHA.
  이 SHA는 `completion` 필드만 제외하므로 서로를 hash하는 순환을 만들지 않는다.
  직접 채팅에서는 원래 요청 바이트의 SHA를 사용한다.

`prepare`는 시작 Git ref·worktree·dirty bytes·stash와 산출물 bytes, 저장소 common-dir
identity, 허용된 원격 identity, 실제 검사 바이너리·환경을 고정한다. 불필요한 전체 Desktop
스캔 대신 작업 소유 위치만 지정한다. baseline 이전 자원은 자동으로 이번 작업 소유가 되지 않는다.
dirty bytes에는 stage된 파일의 index mode·blob·stage와 작업 파일 bytes를 각각 포함한다.
같은 상태 문자열로 다시 stage해도 기존 변경의 손실을 구분한다.

수정할 소유 문서는 처음부터 `git_source`의 파일 목록에 선언한다. `prepare`는 현재 owner
bytes가 HEAD에 보존된 경우 원래 계약의 repository identity·revision·파일·SHA를
`owners[].original`에 고정한다. 최종 result의 `owner_updates`는 그 정확한 owner 경로와
검토된 최종 SHA를 담고, 승인된 삭제는 `null`로 명시한다. `git_source`의 같은 파일 기대값도
`null`로 두며 로컬·원격 기준 커밋과 작업 경로 모두에서 부재를 확인한다. 삭제 표기는 처음부터
수정 대상으로 선언된 owner에만 허용하고 일반 소스 누락·조회 오류는 실패한다.
최종 독립 리뷰가 이 정확한 기대값을 result와 함께 검토한다. 원래 계약·요청·의무를
바꾸지 않으며 선언하지 않은 owner 변경과 예상하지 않은 현재 bytes는 거부한다.
N/A 근거는 고정한 원래 owner를 가리키고, 변경 뒤 타당성도 최종 리뷰에서 확인한다.
Git에 원래 bytes가 없는 기존 dirty owner는 첫 수정 전에 원본 bytes를 대상 저장소 밖의
별도 일반 파일에 보존하고 seed의 `owner_snapshots`에 정확한 owner 경로별 사본 참조를
명시할 수 있다. 사본은 symlink·hardlink가 아닌 1 MiB 이하 파일이며, 현재 owner와 같은
SHA여야 한다. 해당 owner는 여전히 `git_source`에 선언해야 한다.

```json
{
  "owner_snapshots": {
    "/absolute/repository/README.md": {
      "path": "/absolute/retained-originals/README.md",
      "sha256": "SAME_SHA256_AS_THE_CURRENT_OWNER"
    }
  }
}
```

`prepare`는 이 참조를 repository identity·상대 파일 경로와 함께 `owners[].original.snapshot`에
고정한다. Git revision과 사본 중 하나만 원본으로 사용하며 묵시적인 대체는 하지 않는다.
준비·검사는 원본을 쓰거나 Git index를 바꾸지 않는다. 범위 리뷰는 인계·수정 승인과 원본
보존을 확인하고, 이후 `owner_updates`·최종 독립 리뷰·canonical source·baseline/index 보존
검사는 그대로 적용한다. 사본은 매 검사 전후에 다시 확인하므로 삭제·변조하면 실패한다.
사본은 임시 정리 대상으로 예약하지 않고, 원래 scope의 검사·복구가 의존하는 동안 해당
작업 소유자의 근거로 보존한다. 사본 없이 dirty owner를 수정 대상으로 묶는 것은 계속 거부한다.
미변경 계약은 현재 SHA를 계속 대조하며, 이미 고정된 scope에 이 근거를 소급 추가하지 않는다.

독립 검토자가 실제 요청·승인·소유 계약과 이 정확한 범위 파일을 확인한다. 통과 후
`scope_sha256`와 `status: No Findings`를 담은 검토 참조를 `bind`에 전달한다. 이 참조의
형식·hash는 대상 일치만 확인하며, 실제 독립 검토와 의미 판단은 대체하지 않는다.
고정 범위를 바꾸어 실패를 지우지 않는다. 범위 변경·복구가 필요하면 원래 근거를 유지하고
소유자의 재검토를 받는다.

## 고정 이후 외부 owner 변경의 좁은 복구

다른 작업이 수정한 소유 계약 때문에 기존 waiting 세션의 현재 hash 검사가 막혔으면
원래 scope·요청·baseline·의무·예약 자원을 유지한 채 `recover-owner`를 사용한다.
변경된 기존 owner의 정확한 목록을 한 evidence에 고정하고 중복·새 owner를 거부한다.
각 변경 전 bytes와 독립 검토한 현재 bytes를 같은 Git repository identity·정확한 owner 파일의
전체 revision·SHA에 묶는다. 근거는 scope SHA·task/session, 승인 참조, 현재 canonical
main/remote의 `git_source` definition/expected를 포함하고 source 파일 목록에 owner를 넣는다.
독립 리뷰는 scope SHA·recovery evidence SHA·`No Findings`를 정확히 묶는다.

```sh
python3 scripts/completion_hook.py recover-owner --session SESSION_ID --expected-sha256 POINTER_SHA --evidence /absolute/recovery.json --evidence-sha256 RECOVERY_SHA --review /absolute/recovery-review.json --review-sha256 REVIEW_SHA
```

이 명령은 기존 waiting pointer에 그 목록의 evidence/review 참조 한 쌍만 CAS로 추가한다.
원래 scope에 있고 planned-edit `original`이 없는 owner에만 적용하며 scope를 재고정하거나
`owner_updates` 허용 경로를 늘리지 않는다. 상태 근거와 이전 리뷰도 보존하고 재개는
별도 `outcome working`으로 한다. 이미 연결한 recovery를 교체하지 않는다.
최종 리뷰는 기존 scope/result SHA와 추가 `owner_recovery_sha256`를 묶는다.
검사 전후에 원래 owner의 Git bytes, 현재 owner·canonical source와 복구 리뷰를 다시
확인한다. 다른 owner 변경, 새 drift, 원래 의무 실패·살아 있는 임시 자원은 계속 거부한다.
이 hash 형식이 승인·독립 검토의 의미를 대신하지 않으며 선언 안 된 임의 작업을 허용하지 않는다.

원래 owner가 `original` 없는 path/SHA 참조라면 Git 이력 대신 확보된 원문 사본을
명시적으로 결속할 수 있다. owner 항목은 `owner_path`와
`snapshots: {original: {path, sha256}, current: {path, sha256}}`만 담는다.
original 사본의 SHA는 고정 scope의 원래 SHA와 같아야 하고, current 사본의 SHA는
동일 owner 경로의 실제 현재 bytes와 같아야 한다. 두 사본은 owner 경로와 별개인
보존 파일이며 일반 입력의 크기·symlink·hardlink 검사를 그대로 받는다.
원문을 추측하거나 Git 검증 실패 뒤 자동 대체하지 않는다. planned edit은 이 형식을
사용할 수 없다. 기존 scope·권한 근거·독립 리뷰·waiting/CAS·최종 재대조는 유지한다.

## 같은 파일의 타 소유 구간 보존

`git_source`의 기본 검사는 게시된 local/remote main과 작업 파일의 전체 SHA를 대조한다.
동일 파일의 독립된 타 작업 구간을 미커밋 상태로 보존해야 하면, 이를 타 작업의 완료
조건으로 바꾸거나 파일 전체 검사를 끄지 않는다. 원래 `owner_snapshots`와 시작 main에
이미 있던 차이 중 보존할 구간만 별도 evidence와 독립 리뷰로 고정한다.

`preserve-source`는 기존 waiting pointer에 `source_preservation` evidence/review 한 쌍을
CAS로 한 번만 추가한다. 원래 scope·요청·범위 리뷰·상태 근거·자원을 덮어쓰지 않는다.
evidence는 `scope_sha256`, `task_id`, `session_id`, 바로 이전 pointer의
`previous_state_sha256`, `authorization` 경로/SHA, 변경 `reason`, `files`를 담는다.
각 file은 기존 `git_source`의 `check_id`와 `file`, 보존 구간의 타 소유 `owner`·`basis`
경로/SHA 목록, `ranges`를 명시한다. 새 파일이나 의무 누락은 허용하지 않는다.

```json
{
  "check_id": "owned-source",
  "file": "README.md",
  "owner": "the existing independent task",
  "basis": [{"path": "/absolute/ownership-evidence.json", "sha256": "EVIDENCE_SHA256"}],
  "ranges": [{"base": [10, 30], "snapshot": [10, 45]}]
}
```

`base`는 baseline에 고정한 main의 파일, `snapshot`은 해당 owner의 기존 원본 사본을
가리킨다. 구간은 UTF-8 문자나 행 번호가 아닌 **0부터 시작하는 byte 위치 `[시작, 끝)`**다.
변경 부분과 함께 각 파일에서 한 번만 나타나는 정확한 문맥을 포함한다. 구간은 각 원본에서
순서대로 겹침 없이 선언하며, 비어 있거나 모호한 문맥은 거부한다. 소유권은 문맥으로
추정하지 않는다. 기존 승인·소유 근거와 독립 검토가 타 소유 구간의 경계를 판단한다.

독립 리뷰는 정확히 `scope_sha256`, `source_preservation_sha256`, `status: No Findings`를
묶는다. 다음 명령은 evidence가 기록한 바로 이전 pointer SHA를 요구한다.

```sh
python3 scripts/completion_hook.py preserve-source --session SESSION_ID --expected-sha256 POINTER_SHA --evidence /absolute/preservation.json --evidence-sha256 PRESERVATION_SHA --review /absolute/preservation-review.json --review-sha256 REVIEW_SHA
```

재개는 기존 `outcome working`으로 별도 수행한다. 선언을 교체하거나 scope를 다시 bind하지
않는다. 최종 리뷰는 기존 scope/result SHA에 `source_preservation_sha256`도 묶는다.
`owner_recovery`가 함께 있으면 그 SHA도 계속 요구한다.

검사는 게시된 local/remote main의 전체 SHA를 유지하고, 검토된 게시본의 정확한 문맥에만
원래 사본의 보존 구간을 다시 넣어 작업 파일 전체를 대조한다. fuzzy 적용은 하지 않는다.
자기 변경이 미게시 상태이거나, 미등록 차이·타인 구간 손실·문맥 변경·파일 삭제가 있으면
실패한다. baseline의 원래 index blob으로 각 보존 구간의 staged 여부도 확인한다. 시작 main에
선언한 staged 구간을 적용한 bytes가 원래 index 전체와 같아야 한다. 그 밖의 초기 staged 변경은
자기 변경이라도 이 계약에서 묵시적으로 인수하지 않고 정정 연결 전에 거부한다. 현재 index는
게시본에 원래 staged였던 타 소유 구간만 적용한 bytes와 원래 mode를 유지해야 한다.
부분 stage·충돌·모호한 문맥·원래 blob 부재는 거부한다. 같은 파일의 내용 예외로도 타 소유
stage 손실이나 새 stage를 숨길 수 없다. 사본·승인·소유 근거·보존 리뷰는 검사 전후 재확인한다.
이 연결은 baseline/index 변경의 예외를 자동 부여하지 않는다. 기존 정확한 분류·최종 리뷰·
자원·CAS 검사도 유지한다.

## 완료 범주

### 재발 방지 검증

실패를 고치거나 재발 방지를 요청받은 작업은 범위 리뷰에서 `quality`에 `improvement`
검사를 연결한다. “주의하겠다”, 규칙 추가나 파일의 존재만으로 해결을 판정하지 않는다.
개선 요청인데 이 검사가 빠진 범위는 독립 범위 리뷰에서 수정한다. 자연어 의도의 판별과
기준·표본의 적절성은 그 리뷰의 책임이며, 검사기는 선언하지 않은 임의 채팅을 감지하지 않는다.

[check_improvement.py](../scripts/check_improvement.py)는 사전 고정한 JSON 관측의 값과
정확한 입력·후보·실제 사용 파일을 대조한다. 명령·모델·배포를 실행하거나 새 원장을 만들지
않는다. 관측 생성은 원래 도메인의 실행 도구가, 의미·실행의 진실성과 독립성은 기존 리뷰가
담당한다. 결과 파일의 `passed: true` 자기신고를 읽는 방식으로 대체하지 않는다.

1. 기존 작업 기록에서 관측된 문제, 판정 기준과 근거, 수정 전 구현 SHA, 서로 다른 재현·전이
   입력과 수정 전 관측을 준비한다. 기준이 없으면 원래 소유 위치에서 기준을 수립·검토하고
   고정한다. 기준이 없는 채로 완료하거나, 실패한 후보에 맞춰 기대값을 고치지 않는다.
2. 아래 계획을 확인한 뒤 그 정확한 path/SHA를 scope에 묶는다. 재현 사례에서 실제 기준
   위반이 하나 이상 확인돼야 하며, 별도 입력의 전이 사례도 있어야 한다. 실제 도메인 실행이
   없으면 합성 사례라고 밝히며 채용·시청 성과로 표시하지 않는다.
3. 승인된 후보를 원래 도구로 실행해 같은 입력의 수정 후 관측을 만든다. 사례 전체의 고정
   기대값을 통과하고 검토한 후보가 실제 사용 경로와 일치해야 한다. 후보 실패는 기각한다.
4. 최종 완료 리뷰에서 정확한 계획·관측·후보·적용 및 남은 실제 성과 관측 조건을 함께 확인한다.
   다음 실제 관측에서 재발과 결과를 기존 owner 기록에 누적한다. 검증된 수정과 미래 성과
   관측 대기는 구분하며, 예정되지 않은 자동 확인을 약속하지 않는다.

계획의 필수 필드는 `task_id`, `problem`, `criterion`, `basis`(기존 owner 근거 path/SHA),
`baseline_sha256`, `cases`다. 각 case는 `id`, `phase` (`reproduction` 또는 `transfer`),
`input`과 `baseline`(각 path/SHA), `assertions`를 가진다. assertion은 JSON object의 key
경로 `path: ["complete"]`와 고정한 `equals: false` 같은 값 비교다. case·assertion은 각각
최대 32개, key 경로는 최대 16개이며 입력 bytes와 path는 사례 사이에 중복하지 않는다.

각 수정 전후 관측은 `case_id`, `input_sha256`, `implementation_sha256`, `result`를 가진다.
수정 전 implementation은 계획의 baseline SHA, 수정 후는 아래 candidate 참조 목록의
canonical JSON (`sort_keys=True`, separators `(',', ':')`, UTF-8, 추가 newline 없음)의 SHA다.
`result`에는 원래 도구가 실제로 관측한 JSON 값을 넣으며 checker가 assertion을 직접 비교한다.

최종 record는 `plan_sha256`, `candidate`(정확한 source path/SHA 목록), 계획 순서의
`cases: [{"id": "…", "after": {"path": "…", "sha256": "…"}}]`, candidate와 같은 순서의
`application: [{"source": {"path": "…", "sha256": "…"}, "installed_path": "…"}]`를 가진다.
설치 없이 원본 경로를 직접 사용하는 도구는 그 실제 파일 경로를 `installed_path`로 쓴다.
관측과 참조는 일반 UTF-8 JSON 파일이며 원문 값·비밀정보를 공개 Git에 넣지 않는다.

```sh
python3 scripts/check_improvement.py --plan /absolute/plan.json --plan-sha256 PLAN_SHA --task-id TASK_ID
python3 scripts/check_improvement.py --plan /absolute/plan.json --plan-sha256 PLAN_SHA --task-id TASK_ID --record /absolute/record.json --record-sha256 RECORD_SHA
```

첫 명령은 수정 전 계획 검증일 뿐 개선 완료가 아니다. 다음 검사를 범위의 `checks`에 넣는다.

```json
{
  "id": "prevention",
  "category": "quality",
  "kind": "improvement",
  "target": {
    "path": "/absolute/record.json",
    "plan": {"path": "/absolute/plan.json", "sha256": "PLAN_SHA"}
  }
}
```

최종 result의 같은 ID는 `{"sha256": "RECORD_SHA"}`다. 계획·입력·관측의 누락/변조,
재현 실패 부재, 수정 후 실패·전이 회귀, 적용본 불일치나 고정한 검사 result 누락은 완료를
거부한다. 검사 후에도 참조와 적용본을 재대조한다. 값·hash의 일치는 실제 실행·의미 품질,
원인 해결의 일반성이나 미래 성과를 증명하지 않는다. 관측된 사례에서의 통과만 보고한다.

| 범주 | 확인할 결과 |
| --- | --- |
| source | 승인된 변경의 commit, main 반영, 허용된 원격의 현재 ref 및 정확한 변경 파일 보존 |
| usage | 실제 사용하는 설치·링크·빌드·실행본 갱신, 실제 진입점 호출과 필요한 동작 |
| deployment | 운영 배포 필요 여부와 근거; 필요하면 승인 범위·배포본 revision·health·마이그레이션·복구 조건 |
| quality | 변경 위험에 맞는 검증, 현재 정확한 산출물의 독립 코드·문서 리뷰 |
| artifacts | 최종 산출물의 소유 위치와 보존 확인, 임시 복사·payload·log의 정리 |
| git_resources | task local/remote branch, worktree·점유·앱 attachment, stash·미커밋 변경 |
| temporary_resources | 작업이 만든 파일·프로세스 및 자식·탭·서버·세션 자원의 현재 소유 상태 |
| recovery | 취소·중단·외부 side effect·pending apply의 해소 또는 근거를 갖춘 미완료 상태 |

배포가 필요하지 않은 로컬 도구도 배포 범주를 빠뜨리지 않고 소유 계약에 따른 N/A 결정을
검토받는다. main merge만으로 설치·실행·운영 반영을 완료로 인정하지 않는다. 최종 서비스를
작업 임시 서버로 분류해 종료하지 않는다. 보존 예외는 자동 기본값이 아니며 소유자·이유·
범위·제거 조건·현재 근거를 포함하고 최종 보고에 나타난다.

## 진행 중 자원을 놓치지 않기

고정 scope는 현재 pointer 경로를 묶고, 앞으로 생길 모든 ID를 미리 나열하지 않는다.
새 **임시 자원**을 만들기 전에 `reserve`하고, 생성한 뒤 `attach`로 실제 도구가 반환한 경로,
PID와 시작 identity, host handle의 근거를 연결한다. 각 변경 명령은 직전 pointer SHA를
`--expected-sha256`로 받고 잠금 아래 CAS한다. 중복 ID, stale SHA, 예약 없는 attach는 거부된다.

`reserve`/`attach`는 실행·삭제를 하지 않는다. 미해결 예약은 완료를 막는다. 프로세스의
argv가 바뀌어도 같은 시작 시각이면 살아 있는 프로세스로 판단한다. PID 번호만 보고
다른 프로세스를 종료하지 않는다. pointer는 기존 작업 기록에 대한 참조와 현재 handles만
담고 원문·로그·자격증명을 복제하지 않는다. private `.completion/`은 Git에서 제외한다.

pointer는 임시 파일, exact branch ref, 로컬 `refs/stash`, worktree 경로, PID 시작 identity의 부재만 지원한다.
stash는 현재 Git ref를 확인하므로 packed ref와 reflog 없는 ref도 남아 있으면 거부한다.
그 밖의 ref는 범위 준비와 자원 연결 단계에서 거부하며 원격 stash 검사는 지원하지 않는다.
최종 산출물과 유지할 제품 runtime은 pointer에 예약하지 않고, 생성 전 고정 scope의 보존·
설치·health 검사 및 제한된 artifact root로 연결한다. 소유권 이전으로 임시 자원을 유지하려면
기존 예약을 지워 통과시키지 않고 중단 후 정확한 소유자·후속 범위의 재검토를 받는다.
브라우저 탭·managed attachment·authoritative child·native pending apply를 범용 콜백에서
자동 포착했다고 주장하지 않는다. 해당 소유 도구의 실제 조회·검증을 scope의 근거 검사와
독립 검토에 연결해야 한다. pointer에 지원하지 않는 handle을 붙이거나 관측이 불가능하면
미완료다. 읽기 전용 Git 및 제한된 artifact inventory는 새 잔여물뿐 아니라 기존 다른 작업의
삭제·내용 손실도 검출한다. 승인된 변화에는 정확한 resource key의 근거 있는 예외가 필요하다.

## 검증과 종료

작업 소유 도구로 승인된 적용·배포·정리를 수행한다. 원격 branch 삭제는 현재 OID lease,
worktree 정리는 점유·보존 확인과 해당 앱의 archive 경계를 따른다. 다른 작업의 브랜치,
dirty bytes, 산출물을 이 정리의 이름으로 삭제하거나 publish하지 않는다.

최종 result는 고정 scope SHA, 모든 obligation ID의 실제 기대값, 정확한 예외만 담는다.

여러 자원이 같은 보존 판단을 공유하면 optional `exception_groups`에
`{items: [정확한 resource key, ...], classification: 기존 예외 판단}`을 기록한다.
`exceptions`는 기존 개별 예외를 담는다. 각 그룹의 classification은 기존과 같은
owner·reason·scope·removal_condition·basis를 모두 가져야 하며, items는 비어 있지 않은
명시 목록이다. 그룹 안·그룹 사이·개별 예외와의 중복을 거부하고 wildcard나 prefix를
해석하지 않는다. 실제 변경 자원 중 어느 쪽에도 없는 key는 계속 실패한다.
사용되지 않는 명시 key의 처리는 기존 개별 예외와 같다. 원문 1 MiB 제한과 exact result
SHA의 최종 리뷰를 유지하며 보고서도 그룹을 펼치지 않는다. 검사 마지막에 각 판단의
근거 파일을 다시 대조해 검사 중 변조를 거부한다.
소유 도구의 적용·정리 뒤 `draft`로 이 결과의 후보를 수집할 수 있다.
아래 `complete`는 직접 채팅의 종료 명령이다. 큐에서는 `finish --status completed`가
연결된 pointer를 검사하므로 그 전에는 해제하지 않는다.

```sh
python3 scripts/check_completion.py draft --scope /absolute/scope.json --sha256 SCOPE_SHA --output /absolute/result.json
python3 scripts/completion_hook.py complete --session SESSION_ID --expected-sha256 POINTER_SHA --task-id TASK_ID --request-digest REQUEST_DIGEST --result /absolute/result.json --review /absolute/final-review.json --review-sha256 REVIEW_SHA
```

`complete`와 `unbind`에는 현재 마무리할 요청의 `task_id`와 범위 고정 때 묶은
`request_digest`를 명시한다. 세션의 과거 연결만으로 현재 목표를 추정하지 않는다.
전달한 값이 고정 범위와 다르면 pointer 변경 전에 거절하며 원래 연결은 보존한다.
이는 식별값의 일치 검사다. 현재 대화의 의도, 산출물의 의미나 승인 근거의 관련성을
자동 판정하지 않는다.

두 명령 사이에 독립 최종 리뷰를 수행한다. 후보는 파일 bytes와 현재 local/remote main을
수집하며 source 파일 SHA는 local main의 committed bytes를 사용한다. HTTP 검사에는
`--expectations`로 소유자가 선택한 정확한 check ID별 revision·health를 전달해야 한다.
실행 중 endpoint에서 목표 revision이나 정상 상태를 자동 선택하지 않는다. 후보의 예외는
비어 있고, 선언된 owner 변경의 현재 SHA만 `owner_updates`에 담는다. 범위·N/A·승인·리뷰
통과를 만들거나 이미 있는 결과 파일을 덮어쓰지 않는다. 후보 생성은 완료 검사가 아니다.

독립 최종 리뷰는 scope SHA와 **result 파일 SHA**까지 묶어 기대값·예외·owner 변경을
검토한다. 수정 뒤에는 영향받은 부분과 연결 계약을 재검증·재리뷰하며, 정확한 최신 result
SHA의 리뷰 참조를 전달한다. 그대로인 범위의 검증을 반복할 필요는 없다.
`complete`는 잠금과 pointer CAS 아래 검토된 참조를 먼저 보존하고 기존 검사기를 한 번
호출한다. 성공하면 pointer를 제거하고 `binding_released: true`와 현재 관측을 JSON으로
보고한다. 실패·관측 중 변경·미해결 예약이면 pointer와 참조를 남기고 종료 코드 2를 반환한다.
원인을 해소한 뒤 현재 pointer SHA로 재시도한다. 원래 scope·결과·리뷰는 소유 기록에
보존하며 출력은 그 기록의 기존 검증 요약으로 연결한다. 별도 보고 파일을 강제하지 않는다.
보고에는 여덟 범주의 고정 검사·N/A, 기대값, 관측·실패·예외와 정확한 result·리뷰 참조가
함께 나온다. file hash와 N/A 사유의 의미는 계속 소유자와 독립 검토자가 판단한다.

기존 `finalize`, `inspect`, `unbind`는 중간 확인·복구에 계속 사용할 수 있다.
아래 `unbind`는 `finalize`·`inspect`를 거친 연결을 해제하는 대체 경로다.
`complete`가 성공했다면 이미 연결이 해제된 상태다.

```sh
python3 scripts/completion_hook.py unbind --session SESSION_ID --expected-sha256 POINTER_SHA --task-id TASK_ID --request-digest REQUEST_DIGEST
```

큐에서는 원래 request의 `completion`에
scope·범위 리뷰·검사기 SHA를 고정해 제출하고, 실제 `finish`에 `--completion-result`를
전달한다. `finalize`로 최종 참조를 연결한 뒤 `finish`의 완료 전이가 성공하면 `complete`로
현재 자원을 다시 확인하고 세션을 해제한다. result/validation/review 파일 세 개만으로 이
검사를 우회할 수 없다. 큐와 Stop의 기존 검사 출력은 간결하게 유지하며 상세 보고는
`complete`에서 생성한다.

검사기는 네 개 이내 worker로 읽기 전용 관측을 수행하고 마지막에 입력·자원·source를
재대조한다. Git은 사용자 환경의 설정 명령을 상속하지 않으며 네트워크 Git transport를
실행하지 않는다. canonical 로컬 원격은 직접 ref를 읽고, GitHub HTTPS/SSH 원격 identity는
고정된 `gh api` GET으로 읽는다. 다른 transport는 미확인으로 거부한다. private GitHub는
기존 의도된 `gh` 계정의 읽기 권한이 필요하다. 작업 파일 content가 local/remote main에
보존되는지 확인하므로 squash merge도 지원한다. 원격 object는 소유 도구가 먼저 fetch한다.

설치 file hash는 실제 호출·실행 중인 프로그램을 증명하지 않는다. 실제 사용 검증을 별도
owner 근거로 포함한다. HTTP GET은 redirect, 누락 revision/health, 오래된 revision,
불일치를 거부한다. migration과 reviewer independence, N/A 타당성·승인의 의미는 hash로
증명하지 않는다. 검사 중 외부 변경은 다시 검사해야 하며 외부 상태와 큐 DB의 전역 원자성은
보장하지 않는다. 큐는 검사 중 DB lock을 놓고 이후 token·attempt·revision·cancel·route·
시간 및 resource state를 다시 확인한 뒤 완료를 CAS한다.

대기·취소·중단은 `outcome`에 현재 근거를 연결한다. 이 상태를 완료로 표현하지 않고 남은
작업과 해소 조건을 보고한다. 완료 검사가 통과하면 `unbind`가 다시 검사하고 세션 pointer를
제거한다. 취소·blocked 작업의 세션 재사용은 `release`를 사용한다. 원래 scope·result·상태
근거를 기존 소유 기록에 보존하고, 최종 리뷰에 `disposition: stopped`를 명시한다. release는
목표 달성을 주장하지 않고 모든 임시 자원의 부재와 기존 자원 보존·현재 예외를 다시 검사한다.
검증 불가·미해결 예약이면 해제하지 않는다. main 변화도 stop 예외의 검토 대상이다. waiting은
재개할 연결을 유지하고 release하지 않는다. scope 전 취소도 원래 요청·변경 없음·자원을
정확히 검토한 scope를 bind한 뒤 같은 경계로 해제한다. 최종 보고는 main·실제 사용본·배포 결정·잔여 자원·예외를 짧게 제시한다.

## Codex 설치와 실제 연결 확인

`completion_hook.py configure --existing /absolute/hooks.json --output /absolute/candidate.json`은
기존 handler와 순서를 그대로 보존한 후보를 생성한다. 정확한 후보와 스크립트를 검토한 뒤
기존 파일 SHA를 재대조하여 원자적으로 설치한다. 호스트가 지원하는 `/hooks` 검토·신뢰
절차를 거쳐 실제 `hooks/list`에서 enabled/trusted·명령·currentHash를 확인한다.
파일 존재만으로 실행을 완료했다고 보고하지 않는다. 기존 observatory handler를 덮어쓰지 않는다.

추가 LLM·모델 API 호출, Jev 의존성, 상주 polling은 없다. 원격 상태 확인은 필요한
GitHub API 읽기와 HTTP GET을 수행한다. 검사 지연은 실제 대상·환경·원격 호출에
따라 측정해 작업의 기존 검증 기록에 남긴다. 서로 다른 조건의 시간으로 절감률을 만들지 않는다.
Jev가 필요한지는 실제 품질·재작업·비용 비교에서 이 작은 실행 경계로 해결되지 않는 요구가
확인될 때 재평가한다.
