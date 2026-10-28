# meenseek Organization

`meenseek` GitHub 조직의 공개 프로필과 저장소 운영 규칙을 관리합니다.

- [공개 프로필](./profile/README.md)
- [저장소 운영 규칙](./docs/repository-rules.md)

## 시스템 구성과 소유권

`profile/README.md`는 GitHub 조직의 공개 소개를, `docs/repository-rules.md`는 저장소의
소유권·생성·승격·문서 배치 기준을 소유합니다. `AGENTS.md`는 조직 저장소 작업과
로컬 검사 절차를 연결합니다.

[구조 검사기](scripts/check_repositories.py)는 GitHub의 실제 저장소 목록과 선언된
참조의 접근을 대조합니다. 각 저장소의 실제 설치·서비스·native 정책 적용은 해당
소유자와 실행 환경에서 확인하며, 구조 확인을 운영 준비나 사업 성과로 해석하지 않습니다.
operations 등록이나 개인 ontology 사용은 모든 저장소의 필수 조건이 아닙니다.

새 저장소는 [생성 기준의 경량 CI](docs/repository-rules.md#경량-ci)를 함께 적용합니다.
[설치 도구](scripts/install_ci.py)는 대상 workflow와 소유 진입점의 연결을 함께 만들고,
검사기는 CI 파일 발견·선언한 workflow의 현재 commit 실행 결과·과금 차단을 별도로 보고합니다.

검사 명령·입력·결과 해석은 [AGENTS.md의 연결과 검증](AGENTS.md#연결과-검증)을 따릅니다.

## 작업 완료 검사

[정리 검사와 Codex 연결](docs/task-completion.md)은 소유자가 고정한 요청 범위와 현재
Git·사용본·배포 근거·산출물·자원·복구 상태를 읽기 전용으로 확인합니다. operations의
실제 완료 전이는 강제 검사하며 Codex 채팅 훅은 보조 연결입니다. 설치·검증·한계는 해당
문서가 소유합니다. private 정책·승인·도메인 배포 절차를 대신하지 않습니다.

장치 번호만 달라지고 아직 다른 복구를 붙이지 않은 대기 작업은 `completion_hook.py recover-filesystem --help`의 명시적
복구를 사용한다. 원래 scope는 보존하고, 같은 경로·inode의 device 변경 목록과 현재 요청·
이전 pointer·독립 검토를 결합한다. 복구 자체는 완료나 삭제가 아니며 기존 검사를 모두 유지한다.
