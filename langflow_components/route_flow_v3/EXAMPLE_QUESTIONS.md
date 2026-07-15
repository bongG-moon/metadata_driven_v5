# Route Flow V3 예시 질문

## 단일 Tool 실행

- 오늘 WB 공정 생산량을 알려줘.
- 등록된 공정 그룹 도메인을 설명해줘.
- 제품 그룹 집계 규칙을 알려줘.

기대 결과: 필요한 Tool을 한 번만 호출하고 최종 답변을 생성한다.

## 독립 Tool 복수 실행

- 오늘 WB 생산량과 등록된 WB 공정 그룹 정의를 함께 알려줘.

기대 결과: Data Analysis와 Metadata QA를 각각 실행한다. 서로 종속되지 않으므로 두 번째 Tool에 `upstream_result_ref`를 전달하지 않는다.

## 2단계 종속 실행

- 오늘 이상 LOT을 분석하고 해당 LOT의 HOLD 이력을 알려줘.

기대 결과:

1. 이상 LOT Tool을 실행한다.
2. 결과가 `status=ok` 또는 사용 가능한 partial이고 `handoff_usable=true`인지 확인한다.
3. 반환된 `result_ref`를 수정 없이 Data Analysis Tool의 `upstream_result_ref`로 전달한다.
4. 두 결과를 종합한 답변을 한 번만 출력한다.

## 3단계 종속 실행

- 이상 LOT을 찾고 HOLD 이력을 확인한 다음 공정별 주요 HOLD 사유를 집계해줘.

기대 결과: 이상 LOT 조회 -> HOLD 이력 조회 -> HOLD 사유 집계 순서로 실행한다. 각 단계는 바로 앞 단계의 ref가 실제 입력일 때만 전달한다.

## 최대 4단계 실행

- 이상 LOT을 찾고 HOLD 이력을 확인한 뒤 관련 장비의 UPH를 조회하고, 생산 영향이 큰 순서로 정리해줘.

기대 결과: 필요한 경우 최대 4개의 Tool을 순차 호출한다. 동일 Tool과 동일 인자를 반복하지 않고 마지막에만 최종 답변을 작성한다.

## 오류와 참조 불가

- 첫 Tool이 `status=error`이면 그 결과에 의존하는 후속 Tool을 호출하지 않는다.
- 첫 Tool이 성공했어도 `result_ref`가 없거나 `handoff_usable=false`이면 전체 LOT 목록을 자연어에서 추출해 우회하지 않는다.
- 필수 조건이 모호하면 Tool을 호출하지 않고 확인 질문을 한 번만 한다.
