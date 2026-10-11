# meenseek

작게 만들고 직접 써 봅니다. 검증된 것만 공통 기반으로 남깁니다.

제품마다 도메인과 배포를 직접 소유하고, 공통 코드는 반복이 확인된 뒤에 분리합니다.

## 디자인 업무는 이렇게 이어집니다

사용자가 끝내야 할 일을 정하고, 경험을 설계하고, 다른 구조를 비교합니다.
선택한 방향은 실제 화면에서 확인한 뒤 제품에 적용하고 판단 근거를 남깁니다.

| 기준 정하기 | 경험 설계 | 방향 선택 | 화면 제작 | 실제 검증 | 적용·기록 |
| --- | --- | --- | --- | --- | --- |
| 사용자·핵심 행동 | 정보·흐름·상태 | 대안과 선택 이유 | 제품 화면·자산 | 상호작용·작은 화면·키보드 | 사용본과 검증 한계 |

제품은 경험 원칙과 최종 화면을 소유합니다. 공통 React UI 기반은 **Measure Twice**에서
확인할 수 있습니다: [컴포넌트 미리보기][measure-twice-storybook] · [npm 패키지][measure-twice-npm].
실제 적용과 사용자 효과는 각 제품의 검증 근거에서 따로 확인합니다.

## 공개 결과물

- [Ontology](https://github.com/meenseek/ontology) — 개인 지식 온톨로지와 로컬
  컨텍스트 실행 환경. 소스와 이력은 공개하고 운영 데이터는 로컬에 둡니다.
- Measure Twice — React 디자인 시스템의 [npm 패키지][measure-twice-npm]와
  [미리보기][measure-twice-storybook]를 공개합니다.
- [Tarot Spark](https://github.com/meenseek/tarot-spark) — 카드를 뽑고 읽기 방식을
  선택해 AI 해석용 프롬프트를 만드는 타로 제품입니다.

저장소를 나눌 때는 [저장소 운영 규칙][repository-rules]을 따릅니다.

[measure-twice-npm]: https://www.npmjs.com/package/@measure-twice/react
[measure-twice-storybook]: https://measure-twice-design-system.meenseek5929.workers.dev/
[repository-rules]: ../docs/repository-rules.md
