# 저장소 운영 모델

## 목적과 상태

이 문서는 `meenseek` Organization에서 저장소의 소유권과 소비 방식을 판단하는 공개
기준입니다. 미래 구조의 생성 계획이나 일정이 아닙니다.

- 현재 사실은 GitHub의 실제 저장소와 각 저장소의 계약을 확인합니다.
- 후보는 아래 조건을 충족했을 때만 생성합니다.
- 비어 있는 저장소나 패키지를 미래 필요성만으로 미리 만들지 않습니다.
- 각 저장소의 `AGENTS.md`와 실행 가능한 정책이 이 문서보다 구체적인 규칙을
  추가합니다.

## 현재 구조

2026년 8월 13일 기준입니다.

이 공개 목록은 공개 저장소만 열거합니다. 비공개 저장소와 그 이름은 별도 공개
승인이 없으면 의도적으로 생략하며, 목록에서 생략됐다고 해서 부재를 뜻하지는
않습니다. 비공개 저장소도 내부적으로 이 문서의 소유권과 생성 조건을 따릅니다.

```text
github.com/meenseek/
├─ .github                         공개 프로필과 저장소 운영 모델
├─ measure-twice-design-system     React 디자인 시스템
├─ yokgarim                        독립 제품
├─ tarot-spark                     독립 제품
└─ grid-cell                       독립 실험
```

| 저장소 | 상태 | 소비 방식 | 핵심 소유권 |
| --- | --- | --- | --- |
| `.github` | 현재 | 읽기 | 공개 프로필과 Organization 공통 기준 |
| `measure-twice-design-system` | 현재 | 패키지 설치 | React UI 계약과 패키지 릴리스 |
| `yokgarim` | 현재 | 독립 실행·배포 | 한국어 로컬 음성 검토·삐처리, macOS 앱과 제품 릴리스 |
| `tarot-spark` | 현재 | 독립 실행·배포 | 타로 도메인, 제품 코드, 환경과 제품 릴리스 |
| `grid-cell` | 현재 | 독립 실행·배포 | 실험 코드, 환경과 배포 |

## 조건부 확장 구조

다음 이름은 조건을 충족했을 때 사용할 수 있는 경계입니다. 현재 존재한다고
간주하지 않습니다.

```text
github.com/meenseek/
├─ .github/
├─ measure-twice-design-system/
├─ product-foundation/                  조건 충족 시 생성
│  ├─ packages/<promoted-capability>/
│  └─ fixtures/<consumer>/               실제 소비 계약이 필요할 때만 추가
├─ next-product-starter/                 반복되는 프로젝트 생성이 확인되면 생성
├─ automation/                           반복되는 workflow가 확인되면 생성
│  └─ .github/workflows/
├─ yokgarim/
├─ tarot-spark/
├─ grid-cell/
└─ <actual-product-name>/                실제 제품명으로 생성
```

`http`, `ai`, `observability`, `testing`, `next`는 검토할 수 있는 capability 예시일
뿐, 미리 만들 패키지 목록이 아닙니다. 실제 책임은 `http-client`, `next-config`처럼
제공하는 계약이 드러나도록 좁혀야 합니다.

## 소비 방식과 소유권

### Design system

- 제품이 배포된 패키지를 설치합니다.
- 의미 기반 토큰과 범용 UI 컴포넌트를 소유합니다.
- 제품 화면, 비즈니스 컴포넌트와 제품 도메인을 소유하지 않습니다.
- `product-foundation`이나 특정 제품에 의존하지 않습니다.

### Product foundation

- 제품이 버전이 고정된 패키지를 설치합니다.
- 여러 제품에서 검증된 비도메인 런타임 계약만 소유합니다.
- 제품별 DB 모델, 권한 정책, 화면 흐름, 프롬프트와 평가셋을 소유하지 않습니다.
- 제품, starter 또는 consumer fixture를 런타임 의존성으로 참조하지 않습니다.

### Product starter 소유권

- 새 저장소를 만들 때 초기 파일을 한 번 복사합니다.
- 생성된 제품은 복사된 파일과 이후 변경을 직접 소유합니다.
- starter 변경은 기존 제품에 자동 전파되지 않습니다.
- 계속 동기화해야 하는 동작은 패키지나 호출형 workflow에 둡니다.

### Automation 소유권

- 제품 저장소가 검증된 commit SHA의 reusable workflow를 호출합니다.
- 공통 lint, test, build처럼 안정된 job 계약만 소유합니다.
- 호출 저장소가 trigger, 최소 권한, environment, secret과 제품별 배포를
  소유합니다.
- 이동 가능한 기본 브랜치를 중앙 실행 계약으로 사용하지 않습니다.

### Independent product or experiment

- 코드, 도메인, 데이터, 환경, 배포와 제품 릴리스를 직접 소유합니다.
- 공통 계약을 소비할 수 있지만 foundation이 제품을 역으로 참조하지 않습니다.
- 다른 제품의 요구를 대신 수용하는 공통 코드 저장소가 되지 않습니다.

## 의존성 방향

```text
product ─────────> measure-twice-design-system
product ─────────> product-foundation packages
starter ─────────> 배포된 패키지
starter ─────────> 검증된 reusable workflow (automation이 존재할 때)
consumer fixture ─> 검증 대상 foundation package

금지:
foundation production package ─X_runtime─> product / starter / fixture
design system ─X_runtime─> product foundation / product
production package ─X_runtime─> testing package
automation implementation ─X_hardcoded─> specific product path / config
```

패키지 내부에서는 공개 export만 사용합니다. deep import나 순환 의존성을 허용하지
않습니다.

## 생성과 승격 조건

### Foundation package

다음을 모두 충족할 때만 제품에서 `product-foundation`으로 승격합니다.

1. 서로 독립적인 실제 소비자 두 곳 이상에서 거의 같은 구현을 사용합니다.
2. 같은 요구 변경을 두 소비자에 실제로 적용한 기록이 있습니다.
3. 공개 API를 제품 도메인 명사 없이 설명할 수 있습니다.
4. 차이를 작은 명시적 설정으로 설명할 수 있습니다.
5. 독립 버전, 변경 기록, 롤백과 마이그레이션 책임을 정할 수 있습니다.
6. 독립 테스트와 실제 소비 경계의 호환성 검사를 만들 수 있습니다.
7. 추출 후의 릴리스·업그레이드 총비용이 제품별 복사본 유지비보다 낮습니다.

단순 `fetch` wrapper, 제품별 프롬프트, 공급자보다 제품 정책이 중심인 AI 코드와
범용 helper 모음은 이 조건을 충족하지 않습니다.

### Product starter 생성

다음을 모두 충족할 때 생성합니다.

1. 새 프로젝트 생성이 실제로 반복됩니다.
2. 같은 초기 파일과 검증 절차가 반복됩니다.
3. 포함된 앱을 독립적으로 install, lint, test, build할 수 있습니다.
4. 장기 동기화가 필요한 런타임 로직을 포함하지 않습니다.

### Automation 생성

다음을 모두 충족할 때 로컬 workflow에서 추출합니다.

1. 두 저장소 이상에서 같은 안정된 job graph를 사용합니다.
2. 같은 유지보수 변경을 여러 저장소에 실제로 적용한 기록이 있습니다.
3. 중앙 수정의 이익이 공통 장애 전파와 권한 관리 비용보다 큽니다.
4. 최소 소비 fixture와 불변 버전 또는 commit SHA 갱신 절차가 있습니다.

## 환원과 폐기 조건

- 생산 소비자가 없으면 deprecated 후 제거하거나 저장소를 archive합니다.
- 소비자가 하나뿐이고 API가 그 제품 요구에 따라 계속 변하면 제품 내부로
  환원합니다.
- 예외 설정과 escape hatch가 정상 사용보다 많아지면 추상화를 폐기하거나 책임을
  다시 나눕니다.
- starter에 기존 제품과 계속 동기화할 코드가 생기면 패키지 또는 workflow로
  옮깁니다.
- 제품별 배포 정책이 automation에 쌓이면 각 제품 workflow로 돌려보냅니다.

## 문서 갱신 규칙

- 공개 목록은 공개 저장소를 생성·archive하거나 역할과 소비 방식이 바뀔 때
  갱신합니다.
- 비공개 저장소의 변경은 별도 공개 승인 없이는 공개 목록이나 기준일 갱신을
  유발하지 않습니다.
- 현재 상태와 조건부 후보를 한 상태처럼 섞지 않습니다.
- 후보 이름을 실제 존재 증거로 사용하지 않습니다.
- 링크, 공개 패키지와 지원 범위는 실제 GitHub와 registry 상태를 확인한 뒤
  기록합니다.
