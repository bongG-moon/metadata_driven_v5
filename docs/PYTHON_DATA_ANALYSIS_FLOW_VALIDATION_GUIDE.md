# Python 기반 Data Analysis Flow 검증 가이드

이 검증 경로는 Langflow 서버를 실행하지 않고도 v5 Data Analysis Flow의 Python 컴포넌트와 프롬프트를 같은 순서로 실행합니다.

## 검증 범위

1. `.env`의 MongoDB 설정으로 domain, table catalog, main flow filter 메타데이터를 조회합니다.
2. `03_intent_prompt_template_ko.md`와 특화 프롬프트를 사용해 Gemini에서 의도 계획을 생성합니다.
3. 실제 Table Catalog로 조회 작업을 보강하되 데이터 조회 모드는 `dummy`로 고정합니다.
4. `16_pandas_prompt_template_ko.md`로 pandas 코드를 생성합니다.
5. Flow의 안전 실행기에서 코드를 실행하고, 오류가 있으면 기존 코드와 오류를 포함한 1회 repair를 수행합니다.
6. `19_answer_prompt_template_ko.md`로 최종 답변을 생성합니다.

실제 Oracle·Goodocs·H-API는 호출하지 않습니다. MongoDB 메타데이터와 Gemini API만 실제 연결을 사용합니다.

## 사전 설정

저장소 루트 `.env`에 다음 값이 필요합니다.

```dotenv
MONGODB_URI=...
MONGODB_DATABASE=datagov
MONGODB_DOMAIN_COLLECTION=agent_v4_domain_items
MONGODB_TABLE_CATALOG_COLLECTION=agent_v4_table_catalog_items
MONGODB_MAIN_FLOW_FILTER_COLLECTION=agent_v4_main_flow_filters

LLM_PROVIDER=gemini
GOOGLE_API_KEY=...
# 선택 사항: 비어 있으면 gemini-3.5-flash-lite
LLM_MODEL_NAME=
LLM_TEMPERATURE=0
LLM_TIMEOUT_SECONDS=60
```

비밀값은 검증 결과에 출력하지 않습니다.

## 환경과 메타데이터만 확인

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python tools\validate_data_analysis_question.py --check-only
```

`environment.mongodb.loaded_counts`에서 세 메타데이터 건수가 모두 0보다 큰지 확인합니다.

## 질문 한 건 검증

```powershell
python tools\validate_data_analysis_question.py `
  --question "RG 32G DDR4 FBGA 96 DDP 제품 BG공정에서 생산량과 재공수량 알려줘" `
  --reference-date 20260701 `
  --output validation_outputs\rg_bg_result.json
```

## 질문 여러 건 검증

```powershell
python tools\validate_data_analysis_question.py `
  --question "오늘 DA공정 생산량 알려줘" `
  --question "오늘 DA공정에서 생산량이 가장 많은 3개 제품 알려줘" `
  --reference-date 20260701 `
  --output validation_outputs\sample_results.json
```

## 결과 확인 기준

- `status`: 전체 실행 성공 여부
- `metadata_candidates`: 질문에 전달된 metadata 종류별 후보 수
- `intent.plan`: 정규화된 의도 계획과 dataset별 파라미터·필터
- `retrieval.source_results`: dummy 조회 결과와 적용 파라미터
- `pandas.generated_code`: LLM이 만든 실행 코드
- `pandas.repair_attempted`: 오류 후 1회 repair가 수행됐는지 여부
- `pandas.preview_rows`: 실행 결과 일부
- `answer.message`: 사용자에게 반환될 최종 답변
- `semantic_checks`: 실행 성공만으로 발견하기 어려운 공정 그룹 미확장·제품 grain 위반
- `errors`, `warnings`: 실행 차단 오류와 경고

LLM 원문까지 확인해야 할 때만 `--include-raw-responses`를 추가합니다. 기본 결과는 정규화된 계획과 코드만 남겨 payload를 작게 유지합니다.
