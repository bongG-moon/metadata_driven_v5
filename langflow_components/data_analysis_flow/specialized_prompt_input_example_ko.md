제품 속성 token 질문은 일반 제품군 조건이나 단순 pandas filter로 과도하게 분해하지 말고 pandas_function_cases의 product_token_match 케이스를 우선 검토한다.
제품 속성 token은 여러 단어 묶음뿐 아니라 단일 token도 포함한다.
예: "RG 32G DDR4 FBGA 96 DDP", "SP 16G DDR5 2ND X4 78 FCBGA SDP", "DA 16G GDDR6 180".
영문 1자리-숫자 3자리(+선택 영숫자) 패턴의 token은 값이 무엇이든 제품 식별 token이다.
예: "L-123 제품 생산량", "L-218K8H 제품 생산 실적", "A-663 제품", "B-123C1제품", "Q-555A9 제품 재공".
이 패턴의 token만 단독으로 들어와도 product_token_match를 선택한다.
이 패턴의 token 뒤에 제품이라는 말이 붙어도 DEVICE 컬럼 조건이 아니라 제품 식별 token이다. 이런 경우 input_text에는 제품이라는 말을 빼고 패턴 token만 남긴다.
예: "RG 8G DDR4 x16 96 FCBGA SDP, CP 16G DDR x8 78 FCBGA SDP"처럼 콤마로 여러 제품이 들어오면 제품 token 묶음을 그대로 input_text에 남긴다.
예: x16/X8 ORG 표현, FC+숫자/F+숫자 lead 표현, L-218/A-663/B-123처럼 영문 1자리-숫자 3자리(+선택 영숫자) MCP_NO 부분 입력은 match_product_tokens helper가 처리하므로 별도 pandas filter로 과도하게 분해하지 않는다.
lead/ball suffix가 붙은 숫자 표현은 LEAD 제품 속성 token이다. 예: 152ball, 78Lead.
일반 pandas filter로 표현 가능해 보여도 사용자가 제품 식별 token으로 말한 값이면 product_token_match를 선택한다.
사용자가 DEVICE, 디바이스, device code처럼 DEVICE 컬럼을 직접 지칭하지 않는 한, 이 패턴의 제품 식별 token을 DEVICE filter로 만들지 않는다.
단, domain metadata에 등록된 제품군/제품 조건 alias는 product_token_match가 아니라 해당 domain 조건으로 처리한다. 예를 들어 POP제품, MOBILE/모바일 제품, HBM제품처럼 등록된 제품군을 부르는 경우에는 제품 token helper를 선택하지 않는다.
DA공정, D/A공정, WB공정, W/B공정, FCB공정, BG공정처럼 공정명 또는 공정 그룹만 말한 경우는 제품 token 매칭이 아니다.
공정 조건은 match_product_tokens에 넣지 말고 retrieval job의 filters 또는 pandas 전처리 조건으로 OPER_NAME에 적용한다.

두 세부 공정을 `~`, `∼`, `～`, `부터 ... 까지`, `사이`, `구간`, `범위`로 이은 질문은 양 끝 공정만 고르는 조건이 아니라 순서 구간인지 먼저 확인한다.
예: `D/S1~D/A5`는 질문에 적힌 순서와 무관하게 두 label의 숫자 OPER_SEQ 최소값과 최대값 사이를 양 끝 포함해 조회하는 ordered range다.
구간 의미가 명확하고 source에 label/order 역할 컬럼이 있으면 intent_plan.pandas_function_cases에 key=`ordered_process_range`, function_name=`filter_ordered_range`, input_text=사용자의 두 끝점 표현, source_alias=대상 alias를 넣는다.
pandas_execution_plan에도 operation=`apply_pandas_function_case`, function_case_key=`ordered_process_range`, function_name=`filter_ordered_range`, 같은 input_text와 source_alias를 기록한다.
helper 호출 시 실제 schema의 label 컬럼과 order 컬럼을 각각 `label_column`, `order_column`으로 전달한다. 생산/재공 표준 schema에서는 `OPER_NAME`, `OPER_SEQ`를 사용한다.
양 끝점을 `OPER_NAME in [...]`로 바꾸거나 공정 그룹 전체 목록으로 펼치지 않는다. helper가 label에서 각 끝점의 order를 찾은 다음 숫자형 min/max 포함 범위를 적용하게 한다.
끝점이 source label에 없거나, 같은 정규화 label이 서로 다른 order로 연결되거나, label/order 컬럼이 없으면 값을 추측하지 않고 빈 결과로 닫는다.
`D/A1-W/B6`처럼 hyphen 양쪽이 실제 공정 label로 각각 확인될 때만 hyphen을 범위 구분자로 본다. `L-218` 같은 영문 1자리-숫자 3자리 MCP_NO token 내부 hyphen은 범위 기호가 아니므로 filter_ordered_range를 선택하지 않는다.
구분자를 생략해 두 실제 label을 붙여 쓴 표현도 metadata의 ordered range 규칙과 source label lookup으로 두 끝점이 유일하게 확인될 때만 선택한다.

질문에 제품별과 DEVICE/디바이스/device가 함께 나오면 DEVICE만 단독으로 보여주지 않는다.
이 경우 결과 groupby/display 기준에는 DEVICE와 함께 제품 식별 속성도 포함한다.
권장 표시 순서는 TECH, DEN 또는 DENSITY, MODE, ORG, PKG1 또는 PKG_TYPE1, PKG2 또는 PKG_TYPE2, LEAD, MCP_NO, DEVICE, 요청 지표 순서다.
LLM 답변 JSON에 answer_sections.result_table.display_columns를 넣을 수 있으면 위 원본 컬럼명 순서를 사용한다.

장비 배정 정보와 UPH를 함께 요청하면 equipment_assign과 eqp_uph의 실제 공통 모델·공정·Recipe 문맥으로 결합한다.
UPH 상세 결과에는 source에 있는 장비 모델(EQUIP_MODEL), Recipe(RECIPE_ID), 공정(OPER_NAME 또는 OPER_NM)을 공통 필수 문맥으로 유지하고, UPH를 요청한 경우 UPH를 지표 컬럼으로 포함한다.
장비 목록도 요청한 경우에만 장비 ID(EQUIP_ID 또는 EQP_ID)를 포함한다. PRESS_CNT, MCP_NO 등 나머지 속성은 사용자가 직접 요청했거나 해당 분석에 실제로 필요한 경우에만 선택하며 기본 출력으로 강제하지 않는다.
UPH가 장비 모델 또는 Recipe에 따라 다르다고 설명할 예정이면 해당 모델·Recipe·공정 원본 컬럼을 결과에서 제거하지 않는다.

제품 token 매칭이 필요하면 intent_plan.pandas_function_cases 배열에 아래 형식으로 선택 정보를 남긴다.
function_name은 match_product_tokens를 사용한다.
input_text에는 사용자가 말한 제품 속성 token 묶음만 넣고, 날짜/공정/수량 표현은 넣지 않는다.
input_text가 "DA", "D/A", "WB", "W/B", "FCB", "BG", "B/G", "SBM"처럼 공정명/공정 그룹 단독이면 product_token_match를 선택하지 않는다.
source_alias는 helper를 적용할 DataFrame alias를 넣는다.
pandas_execution_plan에는 각 case별로 operation=apply_pandas_function_case, function_case_key, function_name, input_text, source_alias를 포함한다.

제품 token case에서 "DA 16G GDDR6 180"의 DA는 공정 D/A가 아니라 제품 속성 token일 수 있다.
이런 경우 input_text에서 DA를 제거하거나 OPER_NAME=D/A... 필터를 추가하지 않는다.
"오늘 DA공정 생산량"처럼 DA 뒤에 공정이 붙거나 질문 의미가 공정 조건이면 DA는 제품 token이 아니라 공정 그룹이다. 이 경우 product_token_match를 선택하지 않는다.

특화 함수가 여러 개 필요한 예시를 확인해야 할 때만 sample_passthrough_helper를 함께 선택한다.
sample_passthrough_helper는 실제 분석용 helper가 아니며, 여러 function case가 prompt에 전달되는 형식을 확인하기 위한 더미 helper다.

PKG OUT, OUT실적, output 실적은 생산량 metric 표현이다.
metadata에 실제 OPER_NAME="PKG OUT" 공정이 없는 한 공정 필터로 만들지 말고 PRODUCTION 합계로 계산한다.
INPUT, 투입, 투입 실적만 PKG INPUT 공정으로 보며 이때는 OPER_NAME="INPUT" 필터를 사용한다.

질문에 날짜가 없고 생산량, 생산실적, 투입, 재공수량을 현재 기준으로 묻는 경우 table catalog에 당일용 dataset이 있으면 production_today 또는 wip_today를 우선 사용한다.
production 또는 wip 이력 dataset은 어제, 전일, 특정 과거일, EOH, 아침 재공/BOH처럼 이력 기준이 명시된 경우에 사용한다.

아침 재공, BOH, 07시 기준 재공은 wip 이력 데이터의 전일 DATE를 조회한다.
예를 들어 기준일이 20260701이면 오늘 아침 재공 조회 DATE는 20260630이다.
현재 재공, 현시간 기준 재공, 지금 재공은 wip_today를 사용하고 기준일 DATE를 그대로 사용한다.

metadata와 충돌하는 특화 지시는 적용하지 않는다.
table catalog의 required_params는 반드시 data catalog 기준으로만 채운다.
required_params가 아닌 공정/제품/상태 조건은 filters 또는 pandas function case로 남긴다.
