# 누락된 프로세스 식별자 정정

기존 [완료 계약](task-completion.md)의 프로세스 등록은 양의 정수 PID와 실제 시작
식별자를 요구한다. `attach`는 누락·빈 값·형식 오류를 게시 전에 거절한다. 살아 있는
프로세스를 등록할 수 있으며 이 단계에서 종료를 요구하지 않는다.

과거 기록에 `process_absent.target.identity: null`이 이미 들어갔으면, 실제로 관측했던
시작 식별자와 독립 검토를 갖춰 기존 owner의 `repair-process-record`를 사용한다.
현재 PID도 없어야 한다. PID 재사용이나 현재 argv로 과거 식별자를 추측하지 않는다.
이미 값이 있는 식별자, 다른 PID·자원·scope로 바꾸는 기능은 제공하지 않는다.

```sh
python3 scripts/completion_hook.py repair-process-record --session SESSION --expected-sha256 POINTER_SHA --task-id ORIGINAL_TASK --request-digest ORIGINAL_REQUEST_SHA --id RESOURCE_ID --evidence /absolute/repair.json --evidence-sha256 EVIDENCE_SHA --review /absolute/repair-review.json --review-sha256 REVIEW_SHA
```

근거는 정확히 `task_id`, `session_id`, `request_digest`, `scope_sha256`,
`previous_state_sha256`, 기존 `resource` 객체 전체, 실제 `identity`, `source: {path, sha256}`,
`reason`, 기존 `previous_final_review` 참조 또는 `null`을 가진다. `source` 원문 JSON은
관측한 `pid`, `identity`, `source_tool`, `source_call_id`를 가진다. 해당 호출의 식별자 관측만
보존하며 관련 없는 로그나 소스를 복제하지 않는다.

독립 리뷰는 정확히 `scope_sha256`, `evidence_sha256`, `resource_sha256`
(기존 resource의 canonical JSON digest), `previous_state_sha256`, `status: No Findings`를
결속한다. 리뷰는 관측의 실제 출처와 동일 프로세스 여부를 확인한다. 해시 일치만으로
관측의 진실성이나 접근·실행 권한을 부여하지 않는다.

전이는 잠금 아래 현재 pointer SHA를 대조하고 누락된 한 필드만 채운다. 기존 자원·scope·
결과·기록 manifest와 통제 원문을 유지하며, 원래 누락값·관측·판단을 기존 `control_records`에
추가한다. 이전 최종 리뷰는 보존하고 현재 최종 리뷰를 무효화하므로 정정 후 새 최종 리뷰와
원래 완료 검사가 필요하다. 삭제가 이미 시작된 상태는 이 경로로 변경하지 않는다.
