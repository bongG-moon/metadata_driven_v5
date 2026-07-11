# 데이터 분석 flow 특화 입력 가이드

이 파일은 Langflow에서 특화 프롬프트와 특화 함수 값을 어디에 넣어야 하는지 설명한다.

문서 역할:

- `SPECIALIZED_INPUT_GUIDE.md`: Langflow 화면에서 어디에 무엇을 연결/입력하는지 설명한다.
- `function_case_helper_code_input_example.py`: `16 pandas Prompt Template.function_case_helper_code`에 바로 붙여넣을 수 있는 helper 함수 코드다.
- `../../domain_knowledge.txt`: MongoDB에 저장할 function case 선택 metadata 등록용 자연어 지시가 맨 아래에 포함되어 있다.

## 1. 공정 특화 프롬프트 입력 위치

공정/현장별로 임시로 강조해야 하는 해석 규칙이 있으면 아래 위치에 자연어로 입력한다.

바로 복사해서 테스트할 수 있는 값은 `specialized_prompt_input_example_ko.md`에 있다.

| 목적 | 입력 노드 | 입력 포트 | 연결 대상 |
| --- | --- | --- | --- |
| 의도 분석 LLM에 추가 지시 전달 | `Text Input` | `message` | `03 의도 분석 Prompt Template.specialized_prompt` |

입력하지 않아도 된다. 비워두면 기본 metadata와 공통 prompt만 사용한다.

### 입력 예시

```text
W/B, WB, Wire Bond는 같은 공정 그룹으로 해석한다.
아침재공/BOH는 별도 실시간 snapshot이 없으면 전일 WIP 데이터 기준으로 집계한다.
PKG OUT은 제품별 생산실적 중 PKG 완료 조건을 우선 확인하고, metadata에 정의된 조건이 있으면 그 조건을 따른다.
```

주의사항:

- JSON을 넣을 필요가 없다.
- data catalog의 필수 파라미터를 바꾸는 용도로 쓰지 않는다.
- 특정 질문 하나만 맞추기 위한 과도한 fallback 규칙은 넣지 않는다.
- metadata와 충돌하면 metadata를 우선한다.

## 2. 특화 함수 값은 어디에 넣는가

특화 함수는 Langflow 화면에서 pandas 코드 생성 노드에 직접 입력하는 값이 아니다.
또한 실제 함수 구현이나 함수 시그니처를 MongoDB metadata로 저장하지 않는다. `pandas_function_cases` metadata에는 helper 선택 규칙과 helper 이름만 저장한다.

정상 흐름은 아래 순서다.

1. `domain_knowledge.txt` 또는 domain saving flow를 통해 `pandas_function_cases` metadata를 등록한다.
2. `01a MongoDB 도메인 메타데이터 로더`가 해당 metadata를 읽는다.
3. `01d 메타데이터 후보 생성기`가 의도 분석 LLM에 후보로 전달한다.
4. `03 의도 분석 Prompt Template`이 필요한 경우 `intent_plan.pandas_function_cases` 배열을 출력하게 한다.
5. `04 의도 계획 정규화기`가 `pandas_execution_plan` 첫 단계에 `apply_pandas_function_case`를 보강한다.
6. `15 pandas 변수 생성기.function_case_selection_json`은 의도 분석 결과에 들어 있는 function case 선택 정보를 `16 pandas Prompt Template.function_case_selection_json`에 전달한다.
7. 실제 특화 함수 코드는 `function_case_helper_code_input_example.py` 내용을 `16 pandas Prompt Template.function_case_helper_code`에 직접 넣는다.
8. `16 pandas Prompt Template`은 선택 정보의 `selected_steps`와 helper 함수 코드를 함께 보고 생성 pandas 코드 상단에 필요한 함수 정의를 포함한다.
9. `17 pandas 코드 실행기`는 생성된 pandas 코드만 공통 실행기로 실행한다. 특화 helper를 namespace로 제공하지 않는다.

## 3. 현재 지원하는 특화 함수

16번 prompt에 붙여넣을 helper 함수 코드 예시는 `function_case_helper_code_input_example.py`에 있다. 이 파일에는 JSON wrapper 없이 실제 함수 정의만 들어 있다.

```text
function_name: match_product_tokens
signature: match_product_tokens(input_text, frame, token_columns=None, output_order=None)
```

용도:

- `RG 32G DDR4 FBGA 96 DDP`
- `SP 16G DDR5 2ND X4 78 FCBGA SDP`
- `DA 16G GDDR6 180`

처럼 제품 속성이 여러 컬럼에 나뉘어 있고, 사용자가 한 문장 token으로 제품을 말하는 경우에 사용한다.

공정명 또는 공정 그룹 단독 표현은 제품 token 매칭이 아니다. 예를 들어 `DA공정`, `D/A공정`, `WB공정`, `W/B공정`, `FCB공정`, `BG공정`처럼 공정 기준 생산량/재공/실적을 묻는 경우에는 `match_product_tokens`를 선택하지 말고 `OPER_NAME` filter로 처리한다.
`input_text`가 `DA`, `D/A`, `WB`, `W/B`, `FCB`, `BG`, `B/G`, `SBM`처럼 공정명/공정 그룹만 포함하면 `product_token_match`를 선택하지 않는다.

추가 매칭 규칙:

- 콤마로 여러 제품이 들어오면 제품별 token 묶음을 나누고, 제품 묶음끼리는 OR로 결합한다.
- 한 제품 묶음 안의 token들은 모두 매칭되어야 한다. 일부 token만 맞는 행을 제품 매칭 결과로 반환하지 않는다.
- `x16`, `X8`처럼 ORG 앞에 x/X가 붙은 경우 먼저 원문을 매칭하고, 매칭되지 않으면 x/X를 제거해 ORG 값과 매칭한다.
- `FC78`, `FC96`처럼 `FC+숫자` 형태는 `PKG_TYPE1/PKG1=FCBGA`와 `LEAD=해당 숫자`로 해석한다.
- `F78`, `F96`처럼 `F+숫자` 형태는 `LEAD=해당 숫자`로만 해석하고 package type 조건은 추가하지 않는다.
- `152ball`, `78Lead`처럼 숫자 뒤에 `ball` 또는 `lead`가 붙은 표현은 LEAD 조건으로 해석하며, LEAD 비교에서만 suffix를 제거한다.
- `L-218`, `A-663`, `B-123`, `Z-000`처럼 `영문 1자리-숫자 3자리(+선택 영숫자)` 패턴으로 시작하는 입력은 MCP_NO 부분 입력으로 보고 MCP_NO prefix 조건으로 매칭한다.
- `x16`, `X8`처럼 `X+숫자` 형태는 `ORG=해당 숫자`로만 해석한다. LEAD, DEVICE, DEVICE_DESC 등 다른 컬럼으로 fallback하지 않는다.
- 일반 token은 TECH/FAMILY, DEN/DENSITY, MODE, PKG_TYPE1/PKG1, PKG_TYPE2/PKG2, LEAD, MCP_NO, DEVICE 등 구조화 제품 후보 속성 컬럼에 돌려 exact 매칭 여부를 확인한다. DEVICE_DESC는 자유 텍스트 설명 컬럼이므로 token 포함 여부를 보조적으로 확인한다.
- 부분/prefix 조건은 MCP_NO에서만 사용한다. 단, 일반 token이 모든 후보 컬럼 exact 매칭에 실패했다고 해서 MCP_NO로 fallback하지 않는다. MCP_NO로 해석하려면 반드시 `영문 1자리-숫자 3자리(+선택 영숫자)` 패턴을 만족해야 한다.

제품별 DEVICE 표시 규칙:

- 질문에 `제품별`과 `DEVICE`/`디바이스`/`device`가 함께 들어가면 DEVICE 컬럼만 단독으로 보여주지 않는다.
- 결과 groupby/display 기준에 제품 식별 속성도 함께 포함한다.
- 권장 순서는 `TECH`, `DEN`/`DENSITY`, `MODE`, `ORG`, `PKG1`/`PKG_TYPE1`, `PKG2`/`PKG_TYPE2`, `LEAD`, `MCP_NO`, `DEVICE`, 요청 지표 순서다.

```text
function_name: sample_passthrough_helper
signature: sample_passthrough_helper(input_text, frame, note=None)
```

용도:

- 여러 `pandas_function_cases`가 동시에 선택될 때 helper 함수 코드를 여러 개 전달하는 형식을 확인하기 위한 더미 helper다.
- DataFrame을 변경하지 않고 copy를 반환한다.
- 실제 운영 분석에서는 metadata가 명시적으로 선택한 경우에만 사용한다.

Domain Saving Flow에 넣을 raw text는 repo root의 `domain_knowledge.txt` 맨 아래 `pandas function case 등록 규칙` 블록에 포함되어 있다. 이 블록은 helper 구현이 아니라 metadata 등록용 선택 규칙만 담는다.

## 4. 의도 분석 LLM이 출력해야 하는 형태

특화 함수가 필요하다고 판단되면 의도 분석 LLM 응답에 아래 값을 포함해야 한다.
특화 함수가 1개뿐이어도 `pandas_function_cases` 배열에 1개 항목으로 넣는다.

```json
{
  "intent_plan": {
    "pandas_function_cases": [
      {
        "key": "product_token_match",
        "function_name": "match_product_tokens",
        "input_text": "RG 32G DDR4 FBGA 96 DDP",
        "source_alias": "production_data"
      }
    ],
    "pandas_execution_plan": [
      {
        "step": "특화 함수 적용",
        "operation": "apply_pandas_function_case",
        "function_case_key": "product_token_match",
        "function_name": "match_product_tokens",
        "input_text": "RG 32G DDR4 FBGA 96 DDP",
        "source_alias": "production_data"
      }
    ]
  }
}
```

`source_alias`가 여러 개면 각 source에 대해 같은 `input_text`를 적용하도록 단계가 여러 개 생길 수 있다.

특화 함수가 여러 개 필요하면 같은 `pandas_function_cases` 배열에 여러 항목을 넣는다.

```json
{
  "intent_plan": {
    "pandas_function_cases": [
      {
        "key": "product_token_match",
        "function_name": "match_product_tokens",
        "input_text": "RG 32G DDR4 FBGA 96 DDP",
        "source_alias": "production_data"
      },
      {
        "key": "sample_passthrough_demo",
        "function_name": "sample_passthrough_helper",
        "input_text": "format demo",
        "source_alias": "production_data"
      }
    ]
  }
}
```

## 5. pandas Prompt Template에서 사용하는 값

`15 pandas 변수 생성기.function_case_selection_json`은 `16 pandas Prompt Template.function_case_selection_json`에 연결한다. 이 값에는 어떤 helper를 어떤 `input_text`, `source_alias`로 호출해야 하는지가 들어 있다.

`function_case_helper_code_input_example.py` 내용을 그대로 복사해서 `16 pandas Prompt Template.function_case_helper_code` 변수 값으로 붙여넣는다.

특화 함수 코드는 16번에 붙여넣는 `function_case_helper_code`에만 둔다. 15번 출력에는 실제 함수 코드를 넣지 않는다.

pandas LLM은 `function_case_selection_json.selected_steps`가 `match_product_tokens`를 선택하고, `function_case_helper_code`에 해당 helper 함수 정의가 있을 때 아래처럼 helper를 호출한다.

```python
df = match_product_tokens("RG 32G DDR4 FBGA 96 DDP", sources["production_data"])
```

helper 구현은 executor가 제공하지 않는다. `function_case_helper_code_input_example.py`의 함수 정의를 16번 prompt에 넣고, LLM이 그 함수 정의를 생성 pandas 코드 상단에 포함해야 한다.

## 6. 답변 특화 지침 입력 위치

답변 생성에서만 필요한 도메인별 표현 규칙은 공통 `19_answer_prompt_template_ko.md`에 직접 쓰지 않는다.
Langflow Text Input을 하나 만들고 아래처럼 연결한다.

| 목적 | 입력 노드 | 입력 포트 | 연결 대상 |
| --- | --- | --- | --- |
| 답변 LLM에 도메인 특화 표현 규칙 전달 | `Text Input` | `message` | `19 답변 Prompt Template.domain_answer_guidance` |

바로 복사해서 테스트할 수 있는 값은 `answer_domain_guidance_input_example_ko.md`에 있다.

예를 들어 제품 token 매칭 결과를 어떻게 설명할지, 장비 ASSIGN 단계형 분석을 어떤 순서로 말할지는 이 Text Input에 넣는다.
공통 답변 prompt에는 특정 helper 이름이나 특정 제품 token 규칙을 추가하지 않는다.
