# 변경된 소유 계약을 유지한 후속 범위 연결

[작업 완료 계약](task-completion.md)의 `recover-owner`로 검토한 현재 owner를 먼저 결속한 뒤,
`extend`로 추가 의무를 연결할 수 있다. 기존 요청·baseline·검사·자원과 owner 복구의 원래
scope SHA는 그대로 남는다. 새 범위는 기존 범위의 항목을 뒤에 추가하는 형태만 허용한다.

복구가 있는 확장의 일반 범위 리뷰와 delta 리뷰에는 모두 `owner_recovery_sha256`를 추가해
현재 결속된 복구 evidence의 SHA를 고정한다. 다른 필드는 기존 완료 계약을 따른다.
확장 이력에는 그 복구 evidence/review 쌍도 보존하며, 각 복구는 검증된 실제 조상 scope에서
원래 bytes와 리뷰로 확인한다. 다른 작업의 scope나 이력에 없는 SHA는 인정하지 않는다.
복구가 없는 기존 리뷰 형식은 바뀌지 않는다.

같은 원격의 main이 이후 진전해도 검토한 owner 파일들이 모두 같은 SHA이고, 검토한 main의
후손이며 현재 local/remote main이 일치할 때만 그 소유 근거를 재사용한다. owner bytes가
바뀌거나 이 조건이 깨지면 다시 검토해야 한다. 이 판정은 다른 코드 변경의 품질·적용 검사를
대신하지 않는다. `source_preservation`·`filesystem_recovery`와 확장을 동시에 결합하는
범위는 넓히지 않으며 기존 복구 경계를 따른다.
