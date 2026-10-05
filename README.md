# meenseek Organization

`meenseek` GitHub 조직의 공개 프로필과 저장소 운영 규칙을 관리합니다.

- [공개 프로필](./profile/README.md)
- [저장소 운영 규칙](./docs/repository-rules.md)

## 연결과 검증

조직 기준은 [저장소 운영 규칙](docs/repository-rules.md)이 소유합니다.
[검사기](scripts/check_repositories.py)는 GitHub 실제 목록과 선언된 참조의 접근을 확인하고,
각 저장소의 실제 연결은 그 소유 계약과 현재 실행 환경에서 별도로 검증합니다.
operations 등록이나 개인 ontology 사용은 모든 저장소의 필수 조건이 아닙니다.

Python 3.11+와 의도한 계정으로 로그인한 `gh`가 필요합니다. 저장소 루트에서 실행합니다.

```sh
python3 scripts/check_repositories.py --org meenseek --format json
python3 scripts/check_repositories.py --org meenseek --repo .github --format markdown
python3 -m unittest discover -s tests -v
```

명령은 저장소·설정·서비스를 변경하거나 문서의 명령을 실행하지 않습니다.
전체 private 저장소 조회 범위는 조직 admin과 classic token의 repo·조직 읽기 scope를
확인한 경우에만 확인됨으로 표시합니다. 다른 token이나 조회 실패는 접근 가능한 결과와
별개로 전체 범위를 미확인으로 둡니다. 새 자격증명이나 권한 변경을 자동으로 시도하지 않습니다.

JSON에는 확인 시각, 적용한 기준의 SHA-256, 저장소 ID·이름·상태·commit, 진입점과
참조의 확인 결과가 있습니다. 원문 본문은 출력하지 않습니다. 상대 파일과 같은 조직의
현재 기본 브랜치/commit을 가리키는 GitHub 파일 링크는 고정 commit에서 확인합니다.
원문 접근과 내용 판단, anchor, native·외부·과거 commit·호스트 경로는 실행자가 확인합니다.
지원하는 연결 선언은 README 또는 AGENTS 한 곳의 `## 연결과 검증`
(`## Connections and verification`도 허용) 아래 본문과 inline Markdown 링크입니다.
하위 heading은 같은 절에 포함하며 코드 블록의 예시는 실제 참조로 세지 않습니다.
들여쓴 list의 코드·연속 본문처럼 구분이 모호한 문법은 미확인으로 두고 소유자가 판정합니다.
기존 저장소의 동등한 계약은 실행자가 원래 소유 문서로 판정합니다. 지원하는 절이 없다는
미확인만으로 준비 실패를 선언하거나 모든 저장소의 README를 일괄 변경하지 않습니다.

종료 코드 0은 구조 검사 범위만 확인됨, 1은 선언된 참조의 실패, 2는 미확인입니다.
어떤 종료 코드도 서비스 준비나 native 정책 적용의 완료를 뜻하지 않습니다.
권한·네트워크 실패 뒤에는 반환된 범위를 보존하고 다음 승인된 점검에서 재대조합니다.
누적 결과는 기존 작업·일일 점검 기록에 두며 새 목록 DB나 준비 상태 원장을 만들지 않습니다.
