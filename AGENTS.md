# meenseek 저장소 작업 규칙

Canonical GitHub remote owner가 `meenseek`인 저장소에 적용합니다. 대상 저장소의
더 구체적인 로컬 규칙이 있으면 그 규칙이 우선합니다.

## Internal versioning

- 릴리즈 버전은 이 gate의 대상이 아닙니다. 릴리즈 외 내부 버전 번호와 호환 분기는
  만들지 않고 Git history만 사용합니다.
- Migration version은 아래 `Database migrations` 기준의 독립 리뷰가 필수라고 판정하고
  대상 상태, 소유자와 부채 해소·종료 조건을 기록한 경우에만 추가하거나 유지합니다.

## Database migrations

- 새 migration version은 독립 리뷰가 보존해야 하는 기존 database 상태나 이미 적용·배포된
  migration history를 다음 상태로 옮기는 데 필수라고 판정한 경우에만 추가합니다. 대상
  환경·상태, 소유자와 부채 해소·종료 조건을 같은 변경에 기록합니다.
- 비폐기 database에 적용된 적이 없고 보존할 data나 공유된 migration contract가
  없는 초기 초안은 현재 목표 schema의 단일 baseline으로 정리합니다. 존재하지 않는
  upgrade 또는 backfill 경로와 그 호환성 test는 만들지 않습니다.
- 비폐기 database에 적용됐거나 migration history가 배포·공유된 뒤에는 기존
  migration을 수정·삭제·재정렬·squash하지 않고 새 migration을 append-only로
  추가합니다.
- 적용·공유 여부 또는 data 보존 필요를 확인할 수 없으면 baseline을 다시 쓰지 않습니다.
  이 보존은 영구 근거가 아니라 확인 부채이므로 필요한 접근, 소유자와 해소 조건을
  보고합니다.

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
