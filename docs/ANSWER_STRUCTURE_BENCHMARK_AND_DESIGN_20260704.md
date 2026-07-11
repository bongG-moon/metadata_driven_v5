# 현업 사용자용 답변 구조 개선 벤치마크 및 설계안

작성일: 2026-07-04

## 1. 목적

이 문서는 `data_analysis_flow`와 `metadata_qa_flow`의 최종 답변 구조를 현업 구성원이 이해하기 쉬운 형태로 개선하기 위한 기준 문서다.

대상 사용자는 AI 개발자, 데이터 엔지니어, Langflow 운영자가 아니라 제조 현장의 데이터를 조회하고 판단에 활용하는 현업 사용자다. 따라서 답변은 코드, trace, 내부 payload 중심이 아니라 "무엇을 물었고, 어떤 기준으로 계산했고, 결과가 무엇이며, 다음에 무엇을 보면 되는지"를 먼저 보여줘야 한다.

## 2. 외부 서비스 벤치마크 요약

조사 대상은 2026-07-04 기준 대표적인 대화형 BI, 데이터 분석 에이전트, 데이터 카탈로그/거버넌스 서비스다.

| 서비스 | 확인한 답변/UX 패턴 | 우리 flow에 가져올 점 |
| --- | --- | --- |
| Tableau Pulse / Tableau Agent in Pulse | 자연어 질문에 대해 핵심 insight, 시각화, source reference, follow-up question을 함께 제공한다. insight brief는 headings, bullets, summary처럼 스캔 가능한 markdown 구조를 사용한다. | 답변 첫 부분은 핵심 insight로 시작하고, 표/근거/후속 질문은 별도 섹션으로 분리한다. |
| Microsoft Power BI Copilot | 현업 사용자가 report 내용에 대해 질문하거나 요약을 요청할 수 있고, 의미 모델을 기준으로 답변한다. 답변은 visual과 text summary를 같이 제공하며, "How Copilot arrived at this"에서 선택된 field/measure/filter를 검증할 수 있게 한다. | 현업 기본 화면은 답변+표 중심, 개발자/검증 화면은 적용 조건/필드/코드 확인 영역으로 분리한다. |
| ThoughtSpot Spotter | 자연어 질문을 search token으로 변환해 사용자가 이해하고 수정 가능한 형태로 보여준다. 답변은 chart/table과 drill-down을 제공하고, human-in-the-loop feedback으로 정확도를 개선한다. | 내부 pandas code를 먼저 보여주기보다 "사용자가 말한 조건이 어떤 데이터 조건으로 해석됐는지"를 현업 용어로 보여준다. |
| Looker Conversational Analytics | LookML semantic model을 source of truth로 사용하고, 필요한 경우 query result를 분석하거나 Python code execution과 visualization을 활용한다. | 메타데이터와 도메인 정의를 답변의 기준으로 삼고, 분석 결과와 정의가 충돌하지 않도록 한다. |
| Alation | 자연어로 data product에 질문하면 SQL 처리, source citation, lineage, policy, usage를 함께 보여주는 trusted answer를 지향한다. | metadata QA 답변은 "정의", "출처", "필수 조건", "사용 예"를 함께 보여 신뢰할 수 있게 한다. |
| Collibra AI Copilot | 자연어로 data asset을 찾고, business glossary 용어 정의를 즉시 제공하며, RAG로 verifiable context에 답변을 grounding한다. | metadata QA는 임의 추정 없이 저장된 domain/table/main filter metadata만 근거로 답해야 한다. |
| Atlan MCP Chat | 카탈로그에 대해 plain language로 질문하면 asset search, lineage, metadata update, glossary, data quality 같은 도구를 자동 호출한다. | metadata QA는 단순 설명뿐 아니라 "관련 데이터셋/용어/필터를 찾는 안내자" 역할도 해야 한다. |
| Microsoft Purview Unified Catalog | 자연어로 data product를 찾고, metadata와 business context 기반 ranked result를 제공한다. 기술 테이블명보다 업무 목적 중심 discovery를 강조한다. | 현업 질문에는 내부 key보다 업무명, 표시명, 용도, 필수 조건을 우선 보여준다. |

참고 출처:

- Tableau Pulse Help: https://help.tableau.com/current/online/en-us/pulse_intro.htm
- Power BI Copilot Overview: https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-introduction
- Power BI Copilot Q&A: https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-ask-data-question
- ThoughtSpot Spotter: https://docs.thoughtspot.com/cloud/26.6.0.cl/spotter
- ThoughtSpot Spotter AI Analyst: https://www.thoughtspot.com/blog/introducing-spotter-ai-analyst
- Looker Conversational Analytics: https://docs.cloud.google.com/looker/docs/conversational-analytics-overview
- Alation: https://www.alation.com/
- Collibra AI Copilot: https://www.collibra.com/blog/introducing-collibra-ai-copilot-accelerating-asset-discovery-for-all-data-consumers
- Atlan Chat with Your Catalog: https://docs.atlan.com/product/capabilities/atlan-ai/references/mcp-chat-use-cases
- Microsoft Purview Unified Catalog: https://learn.microsoft.com/en-us/purview/unified-catalog-application-card

## 3. 공통 답변 원칙

현업 사용자에게는 아래 순서가 가장 자연스럽다.

1. 질문에 대한 직접 답변
2. 결과를 이해하기 위한 기준과 조건
3. 표 또는 핵심 row preview
4. 사용한 데이터/정의/계산 근거
5. 이상하거나 부족한 점
6. 다음에 물어볼 만한 질문

내부 구현 정보는 기본 답변에 노출하지 않는다.

- 기본 표시: 답변, 결과 표, 적용 기준, 참고/주의, 다운로드
- 개발자 모드: 의도 분석, 데이터 조회 결과, pandas 코드, trace, warnings/errors
- metadata QA 기본 표시: 정의/목록/필수 조건/사용 예
- metadata QA 개발자 표시: metadata key, collection, source document, raw context preview

## 4. Data Analysis Flow 답변 구조 제안

### 4.1 기본 답변 구조

Langflow Chat과 Web/API가 같은 의미 구조를 공유하되, 화면 표현만 다르게 한다.

```text
### 답변
<질문에 대한 직접 답변 1~3문장>

### 결과
<표 또는 핵심 카드>

### 적용 기준
- 기준일:
- 데이터:
- 조회 조건:
- 분석 조건:
- 계산 기준:

### 참고
- 데이터 없음, 더미 데이터, 일부 조건 미매칭, 다운로드 링크 등

### 다음에 볼 만한 질문
- ...
```

Web에서는 같은 정보를 다음 카드 구조로 표현하는 것이 좋다.

| 영역 | 표시 내용 | 기본 노출 여부 |
| --- | --- | --- |
| Answer card | 직접 답변, 핵심 수치, 기준일 | 항상 |
| Result table/card | 결과 rows, row count, 단위 표시 | 항상 |
| Applied criteria | 조회 파라미터, pandas 필터, group by, metric | 접힘 가능 |
| Evidence | 사용 dataset, source type, data_ref, 계산 기준 | 접힘 가능 |
| Process summary | 단계형 분석의 중간 결과 | 중간 결과가 있을 때 |
| Downloads | 결과 CSV, 원본 CSV | data_ref가 있을 때 |
| Developer diagnostics | intent, retrieval, pandas code, warnings/errors | 개발자 모드에서만 |

### 4.2 답변 첫 문장 규칙

첫 문장은 숫자만 말하지 말고 대상, 기준, 결과를 함께 말한다.

나쁜 예:

```text
650입니다.
```

좋은 예:

```text
전일 기준 L-218K8H 제품의 SBM 공정 생산 실적은 650입니다.
```

단계형 분석은 "중간 기준 -> 최종 결과" 순서로 말한다.

```text
현재 재공이 가장 많은 제품은 RG 32G DDR4 FBGA 96 DDP이고, 이 제품의 재공은 12.4K입니다. 해당 제품 기준 장비 ASSIGN 대수는 세부 공정별로 W/B1 3대, W/B2 2대입니다.
```

### 4.3 숫자 표시 정책

원본 데이터와 API 값은 그대로 보존하고, 표시용 메시지에서만 축약한다.

| 값 범위 | 표시 |
| --- | --- |
| 0~9,999 | `9,500`처럼 전체 숫자 |
| 10,000 이상 | `12.4K`, `150K`처럼 K 단위 |
| 비율 | `94.2%` |
| 값 없음 | `0`과 `데이터 없음`을 구분 |

중요한 점은 `0`과 `조회 결과 없음`을 같은 말로 처리하지 않는 것이다.

### 4.4 적용 기준 표시

현업 사용자는 "왜 이 값이 나왔는지"를 SQL이나 pandas code보다 아래 형태로 더 쉽게 이해한다.

```text
### 적용 기준
- 기준일: 2026-07-01
- 사용 데이터: Production History
- 조회 필수 조건: DATE=20260701
- 분석 조건: OPER_NAME=SBM, MCP_NO에 L-218K8H 포함
- 계산 기준: PRODUCTION 합계
```

Data catalog의 `required_params`는 데이터 조회 조건이고, 나머지 공정/제품/상태 조건은 분석 조건으로 구분해서 보여준다.

### 4.5 표 작성 규칙

표는 사용자가 비교해야 하는 항목만 남긴다.

- 컬럼명은 `TOTAL_PRODUCTION`보다 `생산량`처럼 현업 명칭으로 보여준다.
- 기본 preview는 5~10행으로 제한한다.
- 상세 원본은 다운로드 링크로 제공한다.
- 제품별 표는 제품 식별 컬럼을 앞에 둔다: `TECH`, `DEN`, `MODE`, `ORG`, `PKG_TYPE1`, `PKG_TYPE2`, `LEAD`, `MCP_NO`, `DEVICE`.
- 공정별 표는 `공정`, `생산량`, `재공수량`, `ASSIGN 대수`처럼 읽는 순서로 둔다.

### 4.6 제품 token/helper 결과 표시

제품 token 매핑은 공통 프롬프트에 하드코딩하지 않고, 특화 답변 지침으로만 설명한다. 다만 실행 결과가 payload에 들어오면 사용자에게는 아래처럼 "매핑 결과"로 보여준다.

```text
### 제품 매핑
질문 속 제품 표현 `DA 16G GDDR6 180`은 아래 조건의 제품으로 해석했습니다.

| TECH | DEN | MODE | LEAD |
| --- | --- | --- | --- |
| DA | 16G | GDDR6 | 180 |
```

이 섹션은 helper 이름이 아니라 사용자가 말한 제품 표현과 실제 적용된 제품 조건을 보여주는 것이 목적이다.

### 4.7 단계형 분석 표시

단계형 질문은 중간 결과를 숨기면 답변이 부실해 보인다. 따라서 `record_step`으로 저장된 중간 결과는 현업 용어로 요약한다.

예: "현재 재공이 가장 많은 제품기준으로 장비 ASSIGN대수 알려줘, 세부 공정별로"

```text
### 답변
현재 재공이 가장 많은 제품은 RG 32G DDR4 FBGA 96 DDP이고, 재공수량은 12.4K입니다. 이 제품 기준 장비 ASSIGN 대수는 W/B1 3대, W/B2 2대, W/B3 1대입니다.

### 분석 과정 요약
- 1단계: 현재 재공 기준으로 제품별 재공을 집계했습니다.
- 2단계: 재공 1위 제품을 장비 ASSIGN 데이터에 적용했습니다.
- 3단계: 세부 공정별 고유 장비 수를 계산했습니다.
```

### 4.8 개발자 진단 정보 정책

Langflow Playground 검증 단계에서는 의도 분석, 조회 계획, pandas code가 필요하지만 현업 기본 답변에는 과하다.

따라서 현재처럼 `include_diagnostics=false`를 기본값으로 유지하고, 다음 원칙을 둔다.

- 기본 답변: pandas code 미노출
- 개발자 모드: 의도 분석, 데이터 조회, 실제 실행 pandas code 노출
- 오류 발생: 현업 메시지에는 실패 이유와 확인 필요 항목만 표시, 개발자 모드에는 error trace 표시

## 5. Metadata QA Flow 예상 질문과 답변 구조

현재 `data_catalog.txt`, `domain_knowledge.txt`, `main_variable.txt` 기준으로 현업 사용자가 물을 법한 질문은 크게 8가지다.

### 5.1 조회 가능한 데이터 목록 질문

예상 질문:

- 지금 조회 가능한 데이터가 뭐야?
- 생산량, 재공, 장비, LOT 관련 데이터는 어떤 게 있어?
- Goodocs로 가져오는 데이터도 있어?

답변 구조:

```text
### 답변
현재 등록된 주요 데이터는 생산 실적, 재공, 계획, 장비 ASSIGN, 장비 UPH, LOT 상태, HOLD 이력입니다.

### 데이터 목록
| 데이터 | 용도 | Source | 필수 조건 | 주요 수량 |
| --- | --- | --- | --- | --- |
| Production Today | 당일 생산 실적 | Oracle PNT_RPT | DATE | PRODUCTION |
| WIP History | 이력 재공 | Oracle PNT_RPT | DATE | WIP |
...

### 참고
DATE가 필수인 데이터는 질문에 날짜가 필요합니다. HOLD History는 LOT_ID가 필수입니다.
```

### 5.2 특정 데이터셋 설명 질문

예상 질문:

- production_today는 뭐야?
- WIP History는 어떤 데이터야?
- Equipment Assign현황은 어떤 기준으로 장비 대수를 계산해?

답변 구조:

```text
### 답변
Production Today는 당일 생산 실적 질문에 사용하는 Oracle 데이터입니다. 생산량은 PRODUCTION 컬럼 합계로 계산합니다.

### 등록 정보
| 항목 | 내용 |
| --- | --- |
| 표시명 | Production Today |
| 데이터 계열 | production |
| Source | Oracle |
| DB Key | PNT_RPT |
| 필수 조건 | DATE |
| 기준 컬럼 | WORK_DATE |
| 수량 컬럼 | PRODUCTION |

### 이 데이터로 답할 수 있는 질문
- 오늘 DA공정 생산량 알려줘
- 오늘 투입된 제품 중 MCP NO가 L-267로 시작하는 제품의 INPUT 수량 알려줘
```

### 5.3 필수 조건/조회 조건 질문

예상 질문:

- production 조회할 때 꼭 필요한 조건은 뭐야?
- HOLD 이력은 어떤 값을 넣어야 조회돼?
- DATE 형식은 뭐야?

답변 구조:

```text
### 답변
Production History는 DATE가 필수 조건이고, DATE는 YYYYMMDD 형식입니다.

### 필수 조건
| 조건 | 의미 | 형식 | 적용 컬럼 |
| --- | --- | --- | --- |
| DATE | 기준일 | YYYYMMDD | WORK_DATE |

### 주의
Target Goodocs Plan은 DATE가 YYYY-MM-DD 형식으로 저장되어 있어 분석 시 형식 변환이 필요합니다.
```

### 5.4 용어 정의 질문

예상 질문:

- 생산량은 어떤 컬럼으로 계산해?
- 아침 재공이랑 현재 재공은 뭐가 달라?
- INPUT 수량은 어떤 기준이야?
- HBM 제품 조건은 어떻게 등록되어 있어?

답변 구조:

```text
### 답변
아침 재공은 하루 시작 시점의 재공이고, 현재 재공은 현재 시점의 재공입니다. 오늘 아침 재공은 wip_today가 아니라 전일 DATE의 WIP History를 조회해 계산합니다.

### 등록된 정의
| 표현 | 의미 | 사용하는 데이터 | 계산/조건 |
| --- | --- | --- | --- |
| 아침 재공, BOH | 하루 시작 시점 재공 | WIP History | 기준일의 전일 DATE |
| 현재 재공 | 현재 시점 재공 | WIP Today | 기준일 DATE |

### 예시
오늘 아침 07시 기준 DA 16G GDDR6 180 제품 재공 수량 알려줘
```

### 5.5 공정 그룹 질문

예상 질문:

- DA공정에는 어떤 세부 공정이 있어?
- WB공정 차수별이 무슨 뜻이야?
- FCB/H는 FCB에 포함돼?

답변 구조:

```text
### 답변
D/A 또는 DA 공정 그룹은 D/A1~D/A6 세부 공정을 포함합니다.

### 공정 그룹
| 그룹 | 별칭 | 포함 OPER_NAME |
| --- | --- | --- |
| D/A | DA, D/A | D/A1, D/A2, D/A3, D/A4, D/A5, D/A6 |

### 차수 표현
"DA 1차"는 D/A1 단일 공정을 의미하고, "DA공정 차수별"은 D/A1~D/A6를 OPER_NAME별로 나누어 보여달라는 뜻입니다.
```

### 5.6 제품 조건/제품 token 질문

예상 질문:

- HBM 제품 조건은 뭐야?
- Mobile 제품은 어떻게 구분해?
- RG 32G DDR4 FBGA 96 DDP 같은 제품 표현은 어떻게 찾는 거야?

답변 구조:

```text
### 답변
HBM, 3DS, TSV 제품은 TSV_DIE_TYP 값이 비어 있지 않은 제품으로 판단합니다.

### 관련 제품 조건
| 제품 표현 | 적용 조건 |
| --- | --- |
| HBM/3DS/TSV | TSV_DIE_TYP가 존재 |
| Mobile | MODE가 LP로 시작, PKG_TYPE1이 LFBGA/TFBGA/UFBGA/VFBGA/WFBGA, MCP_NO가 비어 있음 |
| POP | MODE가 LP로 시작, PKG_TYPE1이 LFBGA/TFBGA/UFBGA/VFBGA/WFBGA, MCP_NO가 존재 |

### 제품 속성 token
RG 32G DDR4 FBGA 96 DDP처럼 여러 제품 속성이 이어진 표현은 제품 token 매칭 기능을 사용해 TECH, DEN, MODE, PKG, LEAD, MCP_NO 같은 조건으로 해석합니다.
```

### 5.7 SQL/query template 질문

예상 질문:

- 생산량 데이터 관련 쿼리문은 뭐야?
- WIP History 쿼리 보여줘.
- Goodocs 데이터는 어디서 가져와?

답변 구조:

````text
### 답변
Production History는 Oracle PNT_RPT의 이력 생산 실적 source이며, DATE 조건을 WORK_DATE에 적용합니다.

### Query Template
```sql
SELECT ...
FROM PROD_TABLE2
WHERE 1=1
AND WORK_DATE = {DATE}
```

### 필수 조건
| 파라미터 | 적용 컬럼 | 형식 |
| --- | --- | --- |
| DATE | WORK_DATE | YYYYMMDD |
````

SQL은 사용자가 직접 물었을 때만 보여준다. 기본 metadata QA 답변에서 모든 query_template을 자동으로 펼치면 현업 사용자가 읽기 어렵다.

### 5.8 "이 질문은 어떤 데이터로 답해?" 질문

예상 질문:

- 어제 Mobile제품의 PKG OUT실적은 어떤 데이터를 써?
- 오늘 아침재공은 어떤 테이블을 봐?
- 장비 ASSIGN 대수는 어떤 데이터로 계산해?

답변 구조:

```text
### 답변
"어제 Mobile제품의 PKG OUT실적"은 Production History 데이터를 사용하고, 생산량은 PRODUCTION 합계로 계산합니다. Mobile 제품 조건은 MODE/PKG_TYPE1/MCP_NO 조건으로 적용합니다.

### 사용 데이터와 기준
| 항목 | 내용 |
| --- | --- |
| 데이터 | Production History |
| 필수 조건 | DATE=어제 |
| 제품 조건 | Mobile 제품 조건 |
| 공정/수량 기준 | PKG OUT 표현은 PRODUCTION 합계 |

### 바로 물어볼 수 있는 질문
어제 Mobile제품의 PKG OUT실적을 제품별로 알려줘
```

## 6. Metadata QA 답변 정책

metadata QA는 실제 생산량, 재공수량, 투입수량을 계산하지 않는다. 그 질문은 `data_analysis_flow`가 담당한다.

metadata QA가 답해야 하는 것은 아래다.

- 어떤 데이터가 등록되어 있는지
- 어떤 용어가 어떤 컬럼/조건/계산식으로 정의되어 있는지
- 어떤 질문이 어떤 데이터셋과 조건을 사용해야 하는지
- 어떤 필수 파라미터가 필요한지
- query_template, source_type, db_key, Goodocs 문서 ID처럼 사용자가 명시적으로 물은 연결 정보
- 공정 그룹, 제품군, 제품 token, BOH/current WIP 같은 해석 규칙

metadata QA가 기본적으로 숨겨야 하는 것은 아래다.

- raw_trace
- raw_text 전체
- credential
- 전체 MongoDB dump
- 내부 registration trace
- 필요 이상으로 긴 query_template

## 7. 구현 방향 제안

### 7.1 Data Analysis Flow

현재 구조를 크게 바꾸기보다 출력 payload에 "현업 표시용 구조"를 명확히 두는 쪽이 좋다.

권장 payload:

```json
{
  "answer_message": "...",
  "answer_sections": {
    "summary": {
      "headline": "...",
      "basis": ["기준일: ...", "데이터: ..."]
    },
    "result_table": {
      "columns": [],
      "rows": [],
      "row_count": 0
    },
    "applied_criteria": {
      "required_params": {},
      "analysis_filters": {},
      "group_by": [],
      "metrics": []
    },
    "evidence": {
      "datasets": [],
      "calculation_rules": [],
      "step_outputs": [],
      "function_case_results": []
    },
    "next_questions": []
  }
}
```

`answer_message`는 LLM이 작성하고, `answer_sections.result_table`, `applied_criteria`, `evidence`는 deterministic adapter가 payload에서 만든다.

### 7.2 Metadata QA Flow

metadata QA는 질문 유형별 template을 명확히 나누는 것이 좋다.

| 질문 유형 | 답변 template |
| --- | --- |
| 데이터 목록 | 답변 + 데이터 목록 표 + 필수 조건 요약 |
| 데이터셋 설명 | 답변 + 등록 정보 표 + 답할 수 있는 질문 예 |
| 용어 정의 | 답변 + aliases/조건/계산 기준 표 + 주의점 |
| 공정 그룹 | 답변 + 포함 OPER_NAME 표 + 차수 표현 규칙 |
| 제품 조건 | 답변 + 조건 표 + 예시 질문 |
| query template | 답변 + SQL block + required params |
| 이 질문에 필요한 데이터 | 답변 + 데이터/조건/계산 기준 표 + data_analysis로 이어지는 예시 |
| 부적절한 실제 수량 조회 | data_analysis route 안내 |

metadata QA prompt는 자유 서술보다 `answer_type`을 먼저 고르고, 그 유형에 맞는 `answer_sections`를 만들도록 하는 편이 안정적이다.

### 7.3 Web 표시

현업용 web에서는 다음 탭/영역 구성이 좋다.

- `답변`: headline, 핵심 수치, 기준
- `결과`: 표/카드
- `기준`: 사용 데이터, 적용 조건, 계산 기준
- `데이터`: 다운로드 링크, data_ref
- `개발자`: intent, retrieval, pandas code, raw JSON

metadata QA는 다음 구성이 좋다.

- `답변`: 설명 본문
- `등록 정보`: 표
- `사용 예`: 질문 예시
- `관련 항목`: dataset/domain/filter 링크 또는 key
- `원문/SQL`: 사용자가 명시적으로 원할 때만

## 8. 다음 수정 후보

1. `20 답변 응답 생성기`에서 `answer_sections`를 추가한다.
2. `21 답변 메시지 어댑터`가 `answer_sections`를 우선 사용하도록 조정한다.
3. `18 답변 변수 생성기`의 `answer_context_json`에 `applied_criteria`, `next_question_candidates`를 추가한다.
4. `metadata_qa_flow`에 질문 유형별 `answer_type`과 `answer_sections`를 추가한다.
5. Web의 개발자 모드와 현업 기본 표시를 더 명확히 분리한다.
6. 대표 질문과 metadata QA 예상 질문 각각에 대해 "현업 만족도 기준" fixture를 추가한다.

## 9. 설계 판단

가장 중요한 판단은 "현업 기본 답변은 분석가가 설명해주는 말투, 개발자 정보는 접힌 검증 정보"로 분리하는 것이다.

외부 서비스들도 공통적으로 답변을 단순 숫자나 코드로 끝내지 않는다. 핵심 insight를 먼저 보여주고, 시각화/표, 적용된 필드와 필터, source reference, follow-up 질문, governance context를 뒤에 붙인다. 우리 v4도 같은 방향으로 가되, 제조 도메인 특성상 기준일, 공정, 제품 조건, BOH/current WIP 구분, required_params와 분석 필터의 분리를 더 강하게 보여줘야 한다.
