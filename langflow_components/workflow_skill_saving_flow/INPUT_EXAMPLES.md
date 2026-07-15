# Workflow Skill 저장 Flow 입력 예시

아래 문장을 Chat Input에 그대로 넣을 수 있습니다. 최초 실행은 `dry_run=true`로 확인하고, 결과가 맞으면 `false`로 바꿔 실제 저장합니다.

## 1. 생산량 조회 후 메타데이터 정의 조회

```text
Workflow Skill을 하나 등록해줘.
key는 wb_daily_production_metadata이고 표시 이름은 "WB 당일 생산량과 공정 그룹 정의 조회"야.
사용자가 "오늘 WB 생산량과 등록된 WB 공정 정의를 함께 알려줘"처럼 요청할 때 사용해.
먼저 data analysis로 오늘 WB 공정 생산량을 조회하고, 그 실행이 끝난 뒤 metadata qa로 등록된 WB 공정 그룹 정의와 포함 공정을 조회해.
두 번째 단계는 첫 단계 결과 행을 사용하지 않고 실행 순서만 보장하면 돼.
첫 단계 오류 시 중단하고 두 번째 단계 오류는 나머지 결과를 보여줘.
키워드는 WB, 생산량, 공정 그룹이고 우선순위는 100이야.
```

예상 핵심 계약:

- `production`: `run_data_analysis`, `depends_on=[]`, `handoff=none`
- `metadata`: `run_metadata_qa`, `depends_on=[production]`, `handoff=none`

## 2. 이상 LOT 결과를 HOLD 이력 조회에 전달

```text
key anomaly_lot_hold_history로 Workflow Skill을 등록해줘.
이름은 "이상 LOT과 HOLD 이력 연계 조회"이고, 사용자가 이상 LOT을 분석하고 그 LOT의 HOLD 이력까지 요청할 때 선택해.
1단계 anomaly_lots에서는 data analysis로 오늘 이상 LOT을 조회하고 LOT_ID가 결과에 포함되게 해.
2단계 hold_history에서는 1단계가 성공한 뒤 그 결과에 포함된 실제 LOT만 대상으로 data analysis에서 HOLD 이력을 조회해.
2단계에는 1단계의 result_ref를 전달하고 두 단계 중 하나라도 실패하면 중단해.
호출 예시는 "오늘 이상 LOT을 찾고 해당 LOT의 HOLD 이력을 알려줘"야.
```

예상 핵심 계약:

- 두 단계 모두 `run_data_analysis`
- `hold_history.depends_on=[anomaly_lots]`
- `hold_history.handoff=result_ref`

## 3. 도메인 저장 후 Metadata QA 확인

```text
key domain_registration_then_verify로 Workflow Skill을 등록해줘.
표시 이름은 "도메인 등록 후 확인"이야.
사용자가 새로운 공정 그룹 정의를 등록하고 바로 확인해 달라고 명시적으로 요청할 때만 사용해.
먼저 save_domain_metadata Tool로 사용자가 제공한 공정 그룹 정의를 저장하고, 저장이 끝나면 run_metadata_qa Tool로 같은 공정 그룹의 등록 내용을 확인해.
실제 데이터 결과 전달은 필요 없으므로 handoff는 사용하지 마.
저장 단계가 실패하면 중단해.
제외 키워드는 "조회만", "저장하지 마"야.
```

이 예시는 저장 의도가 명시된 요청에만 맞도록 `intent_examples`와 `excluded_keywords`를 구체적으로 작성하는 사례입니다.

## 4. 최대 4단계 독립 조회 Workflow

```text
key daily_manufacturing_briefing으로 "일일 제조 브리핑" Workflow Skill을 등록해줘.
1단계 production은 오늘 DA 공정 생산량을 data analysis로 조회해.
2단계 wip은 1단계가 끝난 뒤 오늘 DA 공정 재공을 data analysis로 조회하지만 앞 결과 행은 전달하지 마.
3단계 equipment는 2단계가 끝난 뒤 현재 DA 공정 장비와 UPH를 data analysis로 조회하고 앞 결과 행은 전달하지 마.
4단계 metadata는 3단계가 끝난 뒤 metadata qa로 위 조회에 사용된 데이터 소스 등록 정보를 조회해.
모든 단계는 순차 실행하고, 1~3단계 오류는 중단하며 마지막 메타데이터 오류만 continue로 처리해.
호출 예시는 "오늘 DA 공정 브리핑을 만들어줘"야.
```

각 단계는 `depends_on`으로 직전 단계를 참조하지만 모두 `handoff=none`입니다.

## 5. 기존 Skill을 replace로 갱신

노드에서 `duplicate_action=replace`를 선택하고 다음을 입력합니다.

```text
기존 wb_daily_production_metadata Workflow Skill을 교체해줘.
key와 표시 이름은 그대로 유지하고, 생산량 조회 뒤 메타데이터 QA에서 공정 그룹 정의뿐 아니라 사용 데이터 소스도 함께 조회하도록 두 번째 질문을 변경해.
호출 예시는 "오늘 WB 생산량과 그 데이터 소스를 알려줘"를 추가해.
단계는 두 개이고 실제 결과 ref는 전달하지 않아.
```

- 유사 문서 1건: 기존 canonical `_id`를 유지하면서 내용 교체
- 유사 문서 없음: 입력 key로 신규 저장
- 유사 문서 여러 건: 대상 자동 선택 없이 저장 차단

## 6. 검증 차단 예시

```text
Workflow를 등록해줘.
1단계는 metadata qa로 등록된 테이블 목록을 조회해.
2단계는 1단계의 result_ref를 받아 data analysis로 생산량을 조회해.
```

`run_metadata_qa`는 `result_ref`를 생성하지 않으므로 `result_ref_source_not_supported` 또는 `invalid_result_ref_contract`로 저장이 차단되어야 정상입니다.

## API 실행 입력 예시

Flow API의 Chat Input node ID가 `ChatInput-workflow_skill`인 경우의 예시입니다. Import 후 실제 node ID가 다르면 Langflow API 예시 화면에서 확인한 ID로 바꿉니다.

```json
{
  "input_value": "key wb_daily_production_metadata로 오늘 WB 생산량 조회 후 등록된 WB 공정 그룹 정의를 조회하는 Workflow Skill을 등록해줘.",
  "input_type": "chat",
  "output_type": "chat",
  "tweaks": {
    "Request-workflow_skill": {
      "duplicate_action": "replace",
      "dry_run": true
    }
  }
}
```
