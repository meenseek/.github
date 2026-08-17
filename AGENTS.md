# meenseek 저장소 작업 규칙

Canonical GitHub remote owner가 `meenseek`인 저장소에 적용합니다. 대상 저장소의
더 구체적인 로컬 규칙이 있으면 그 규칙이 우선합니다.

## Database migrations

- 새 migration version은 구현 단계의 기록이 아니라, 보존해야 하는 기존 database
  상태나 이미 적용·배포된 migration history를 다음 상태로 옮길 때만 추가합니다.
- 비폐기 database에 적용된 적이 없고 보존할 data나 공유된 migration contract가
  없는 초기 초안은 현재 목표 schema의 단일 baseline으로 정리합니다. 존재하지 않는
  upgrade 또는 backfill 경로와 그 호환성 test는 만들지 않습니다.
- 비폐기 database에 적용됐거나 migration history가 배포·공유된 뒤에는 기존
  migration을 수정·삭제·재정렬·squash하지 않고 새 migration을 append-only로
  추가합니다.
- 적용·공유 여부 또는 data 보존 필요를 확인할 수 없으면 baseline을 다시 쓰지 않고
  확인되지 않은 상태를 보고합니다.

## Branch

- 이름: `<type>-<lowercase-kebab-case>`
- `type`: `feature`, `fix`, `docs`, `chore`, `refactor`, `test`, `build`, `ci`,
  `perf`
- 하나의 검토 가능한 변경 범위만 담습니다.

## Commit

- 제목: `<type>: <imperative summary>` 또는 `<type>(<scope>): <imperative summary>`
- `type`: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `build`, `ci`,
  `perf`, `revert`
- 제목 끝에 마침표를 붙이지 않습니다.
- 독립적으로 이해하고 되돌릴 수 있는 단위로 나눕니다.
- 본문: 변경, 이유, 경계, 의사결정 출처, 검증

## Pull request

- 제목은 commit 제목 형식을 따릅니다.
- 본문은 commit 본문 항목에 미실행 검증과 위험·복구 방법을 추가합니다.
