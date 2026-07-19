# Workflow Skill 저장 Flow 입력 예시

아래 4개는 현재 **08 Workflow Orchestrator**에 실제 연결된 `run_data_analysis`, `run_metadata_qa`, `run_visualization`을 사용하는 실행용 Skill입니다. 마지막 기본 Language Model은 Tool이 아니라 08의 공통 최종 합성 단계이므로 Skill `steps`에 별도로 추가하지 않습니다.

## 등록 방법

1. 아래 등록 문장을 **09 Workflow Skill Saving**의 Chat Input에 붙여 넣습니다.
2. 먼저 `dry_run=true`, `duplicate_action=replace`로 결과를 확인합니다.
3. 내용이 맞으면 `dry_run=false`로 다시 실행해 MongoDB에 저장합니다.
4. 저장 후 **08 Workflow Orchestrator**에 Workflow key 또는 실행 질문을 입력합니다.

| Workflow key | Data Analysis | Metadata QA | 최종 기본 LLM |
| --- | ---: | ---: | ---: |
| `daily_manufacturing_briefing` | 2회 | 1회 | 1회 |
| `hold_lot_history_metadata_audit` | 2회 | 1회 | 1회 |
| `equipment_uph_source_audit` | 1회 | 1회 | 1회 |

## 1. DA 일일 생산·재공 브리핑

09 등록 입력:

```text
key daily_manufacturing_briefing으로 Workflow Skill을 등록해줘.
표시 이름은 "DA 일일 생산·재공 브리핑"이야.
설명은 오늘 DA 공정의 생산량과 현재 재공을 순차 조회하고 사용 데이터셋의 등록 정보를 확인한 뒤 기본 LLM이 하나의 브리핑으로 종합하는 업무야.
별칭은 "DA 일일 제조 브리핑", "오늘 DA 운영 현황", "DA 생산 재공 데이터 소스"야.
호출 예시는 "오늘 DA 공정의 생산량과 현재 재공, 사용 데이터 소스를 함께 알려줘", "DA 공정 일일 생산·재공 브리핑을 만들어줘"야.
키워드는 DA 운영 브리핑, DA 생산량과 재공, 생산 재공 데이터 소스, 일일 제조 브리핑이야.
제외 키워드는 등록해줘, 저장해줘, 변경해줘, 삭제해줘, HOLD 이력, 장비 UPH야.
우선순위는 100이야.

1단계 production은 run_data_analysis로 오늘 D/A 공정 그룹(D/A1~D/A6)의 세부 공정별 생산량을 조회해.
첫 단계이므로 depends_on은 비우고 handoff는 none, 오류 시 stop이야.

2단계 wip은 run_data_analysis로 현재 D/A 공정 그룹(D/A1~D/A6)의 세부 공정별 재공 수량을 조회해. LOT 또는 랏 요청이 아니므로 lot_status가 아닌 wip_today 데이터를 사용해.
production 실행 뒤에 시작하고 앞 결과 행은 사용하지 않으므로 depends_on은 production, handoff는 none, 오류 시 stop이야.

3단계 metadata는 run_metadata_qa로 메타데이터에 등록된 production_today와 wip_today의 데이터셋 이름, 데이터 소스 유형, 필수 파라미터, 수량 컬럼을 비교해서 알려줘.
wip 실행 뒤에 시작하고 앞 결과 행은 사용하지 않으므로 depends_on은 wip, handoff는 none, 오류 시 continue야.
```

08 실행 확인 질문:

```text
daily_manufacturing_briefing
```

```text
오늘 DA 공정의 생산량과 현재 재공, 사용 데이터 소스를 함께 브리핑해줘.
```

## 2. 현재 HOLD LOT 이력과 데이터 정의 감사

09 등록 입력:

```text
key hold_lot_history_metadata_audit로 Workflow Skill을 등록해줘.
표시 이름은 "현재 HOLD LOT 이력과 데이터 정의 감사"야.
설명은 현재 HOLD 중인 LOT을 조회하고 실제 LOT 결과를 HOLD 이력 분석에 전달한 뒤 관련 데이터셋의 등록 정의를 확인해 기본 LLM이 종합하는 업무야.
별칭은 "HOLD LOT 이력 감사", "현재 HOLD와 이력", "HOLD LOT 원인 조회"야.
호출 예시는 "현재 HOLD 중인 LOT과 해당 LOT의 HOLD 이력, 사용 데이터 소스를 함께 알려줘", "HOLD LOT의 최근 이력과 관련 메타데이터를 감사해줘"야.
키워드는 현재 HOLD LOT, HOLD 이력과 원인, LOT HOLD 감사, HOLD 데이터 소스야.
제외 키워드는 등록해줘, 저장해줘, 변경해줘, 삭제해줘, 장비 UPH야.
우선순위는 105야.

1단계 current_hold_lots는 run_data_analysis로 현재 HOLD 중인 LOT의 LOT_ID, 공정, UNIT 수량, Wafer 수량, 현재 TAT와 누적 TAT를 조회해.
첫 단계이므로 depends_on은 비우고 handoff는 none, 오류 시 stop이야.

2단계 hold_history는 run_data_analysis로 이전 결과에 포함된 실제 LOT만 대상으로 HOLD 이력을 최신순으로 조회하고 HOLD 코드, 상세 사유, 발생 시간을 보여줘.
current_hold_lots의 실제 결과가 필요하므로 depends_on은 current_hold_lots 하나, handoff는 result_ref, 오류 시 stop이야.

3단계 metadata는 run_metadata_qa로 메타데이터에 등록된 lot_status와 hold_history 데이터셋의 용도, 데이터 소스 유형, 주요 식별 컬럼과 HOLD 관련 컬럼을 비교해서 알려줘.
hold_history 실행 뒤에 시작하지만 앞 결과 행은 사용하지 않으므로 depends_on은 hold_history, handoff는 none, 오류 시 continue야.
```

08 실행 확인 질문:

```text
hold_lot_history_metadata_audit
```

```text
현재 HOLD 중인 LOT과 해당 LOT들의 HOLD 이력, 사용 데이터 소스를 함께 알려줘.
```

## 3. D/A1 장비·UPH와 데이터 소스 감사

09 등록 입력:

```text
key equipment_uph_source_audit로 Workflow Skill을 등록해줘.
표시 이름은 "D/A1 장비 UPH와 데이터 소스 감사"야.
설명은 현재 D/A1 장비와 Recipe별 UPH를 조회하고 장비 배정 및 UPH 데이터셋의 등록 정의를 확인한 뒤 기본 LLM이 함께 정리하는 업무야.
별칭은 "D/A1 장비 UPH 감사", "DA1 UPH 데이터 소스", "장비 UPH 메타데이터 확인"이야.
호출 예시는 "현재 D/A1 장비와 UPH, 사용 데이터 소스를 함께 알려줘", "D/A1 장비 모델과 Recipe별 UPH를 조회하고 관련 메타데이터도 확인해줘"야.
키워드는 D/A1 장비 UPH, 장비 UPH 데이터 소스, Recipe UPH 메타데이터, 장비와 UPH야.
제외 키워드는 등록해줘, 저장해줘, 변경해줘, 삭제해줘, HOLD 이력이야.
우선순위는 105야.

1단계 equipment_uph는 run_data_analysis로 현재 D/A1 공정에 배정된 장비의 장비 ID, 장비 모델, Recipe, 공정과 UPH를 조회해.
첫 단계이므로 depends_on은 비우고 handoff는 none, 오류 시 stop이야.

2단계 source_metadata는 run_metadata_qa로 메타데이터에 등록된 equipment_assign과 eqp_uph 데이터셋의 용도, 데이터 소스 유형, 필수 파라미터, 연결 기준 컬럼과 주요 출력 컬럼을 비교해서 알려줘.
equipment_uph 실행 뒤에 시작하지만 앞 결과 행은 사용하지 않으므로 depends_on은 equipment_uph, handoff는 none, 오류 시 continue야.
```

08 실행 확인 질문:

```text
equipment_uph_source_audit
```

```text
현재 D/A1 장비와 장비 모델, Recipe별 UPH를 보여주고 어떤 데이터 소스를 사용했는지도 알려줘.
```

## 4. 최근 D/A 생산량 HTML 차트

09 저장 Flow의 Chat Input에 다음처럼 입력합니다.

```text
Workflow Skill 한 건을 등록해줘.
section은 workflow_skills이고 key는 recent_da_production_chart, status는 active야.
표시 이름은 최근 D/A 생산량 HTML 차트야.
설명은 최근 3일 D/A 공정 생산량을 일자별로 조회한 뒤 선 그래프 HTML로 시각화하는 업무야.
별칭은 D/A 생산량 차트, 최근 DA 생산 그래프야.
호출 예시는 "최근 3일 D/A 공정 생산량을 조회하고 그래프로 그려줘", "DA 생산량 추이를 HTML 차트로 보여줘"야.
키워드는 D/A, 생산량, 최근 3일, 차트, 그래프야.
제외 키워드는 등록해줘, 저장해줘, 변경해줘, 삭제해줘야.
우선순위는 100이야.

1단계 production은 run_data_analysis로 최근 3일 D/A 공정 생산량을 일자별로 집계해.
첫 단계이므로 depends_on은 비우고 handoff는 none, 오류 시 stop이야.

2단계 chart는 run_visualization으로 일자를 X축, 생산량을 Y축으로 사용한 선 그래프 HTML을 만들어줘.
production의 실제 결과 행이 필요하므로 depends_on은 production 하나, handoff는 result_ref, 오류 시 stop이야.
```

08 실행 확인 질문:

```text
최근 3일 D/A 공정 생산량을 조회하고 그래프로 그려줘.
```

## 실행 계약

- `depends_on`은 실행 순서만 보장합니다.
- `handoff=result_ref`는 앞 `run_data_analysis`의 실제 결과 행이 다음 `run_data_analysis` 또는 `run_visualization`에 필요할 때 사용합니다.
- `run_metadata_qa`에는 앞 단계 결과 행을 넘기지 않고 `handoff=none`으로 실행합니다.
- `run_visualization`은 첫 단계로 실행하지 않으며, 바로 앞 분석 단계 하나를 `depends_on`으로 참조합니다.
- 마지막 기본 Language Model이 성공한 단계 결과와 경고를 한 답변으로 정리합니다.
- 저장·등록·변경 요청은 위 4개 조회 Skill에 매칭하지 않습니다.

## API 등록 입력 예시

Flow API의 Chat Input node ID가 `ChatInput-workflow_skill`인 경우입니다. Import 후 실제 node ID가 다르면 Langflow API 예시 화면의 ID를 사용합니다.

```json
{
  "input_value": "key equipment_uph_source_audit로 D/A1 장비와 UPH를 조회하고 관련 데이터셋 정의를 확인하는 Workflow Skill을 등록해줘.",
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
