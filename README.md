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

검사 명령·입력·결과 해석은 [AGENTS.md의 연결과 검증](AGENTS.md#연결과-검증)을 따릅니다.

## 작업 완료 검사

[정리 검사와 Codex 연결](docs/task-completion.md)은 소유자가 고정한 요청 범위와 현재
Git·사용본·배포 근거·산출물·자원·복구 상태를 읽기 전용으로 확인합니다. operations의
실제 완료 전이는 강제 검사하며 Codex 채팅 훅은 보조 연결입니다. 설치·검증·한계는 해당
문서가 소유합니다. private 정책·승인·도메인 배포 절차를 대신하지 않습니다.
