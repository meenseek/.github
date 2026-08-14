# meenseek Git Workflow

Canonical GitHub remote owner가 `meenseek`인 저장소에서 branch 이름은 `Branch`,
commit message는 `Commit`, pull request 제목·본문은 `Commit`과 `Pull request`
섹션을 적용합니다. 대상 저장소의 더 구체적인 로컬 규칙이 있으면 그 규칙이
우선합니다.

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
