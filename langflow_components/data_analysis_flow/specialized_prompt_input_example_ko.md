제품 속성 token 질문은 일반 제품군 조건이나 단순 pandas filter로 과도하게 분해하지 말고 pandas_function_cases의 product_token_match 케이스를 우선 검토한다.
사용자가 `제품별`, `제품 기준`, `상위 N개 제품`, `하위 N개 제품`, `가장 많은 제품`, `가장 적은 제품`처럼 제품 단위 집계나 순위를 요청하면 후보 Domain에서 `section=product_key_columns`, `key=standard_product_keys` 항목을 필수로 선택한다.
선택한 `standard_product_keys`는 `metadata_refs`와 `intent_plan.grain_plan.metadata_ref`에 기록하고, 실제 제품 집계 컬럼은 해당 metadata의 columns와 table catalog mapping으로 해석한다.
`analysis_recipes:product_grain_and_join_policy`는 제품 집계·결합 정책을 보완할 수 있지만 제품 단위 집계의 `standard_product_keys` 참조를 대신하지 않는다.
후보 Domain에 `standard_product_keys`가 없으면 제품 키 컬럼을 추측하거나 DEVICE를 대신 사용하지 말고 clarification으로 보낸다.
제품 속성 token은 여러 단어 묶음뿐 아니라 단일 token도 포함한다.
예: "RG 32G DDR4 FBGA 96 DDP", "SP 16G DDR5 2ND X4 78 FCBGA SDP", "DA 16G GDDR6 180".
영문 1자리-숫자 3자리(+선택 영숫자) 패턴의 token은 값이 무엇이든 제품 식별 token이다.
예: "L-123 제품 생산량", "L-218K8H 제품 생산 실적", "A-663 제품", "B-123C1제품", "Q-555A9 제품 재공".
이 패턴의 token만 단독으로 들어와도 product_token_match를 선택한다.
이 패턴의 token 뒤에 제품이라는 말이 붙어도 DEVICE 컬럼 조건이 아니라 제품 식별 token이다. 이런 경우 input_text에는 제품이라는 말을 빼고 패턴 token만 남긴다.
이 구조화 token을 DEVICE_DESC, DEVICE 또는 자유 텍스트 contains filter로 바꾸지 않는다. 해당 source에 그런 컬럼이 보이거나 보이지 않는지와 무관하게 product_token_match helper의 input_text로 전달한다.
예: "RG 8G DDR4 x16 96 FCBGA SDP, CP 16G DDR x8 78 FCBGA SDP"처럼 콤마로 여러 제품이 들어오면 제품 token 묶음을 그대로 input_text에 남긴다.
예: x16/X8 ORG 표현, FC+숫자/F+숫자 lead 표현, L-218/A-663/B-123처럼 영문 1자리-숫자 3자리(+선택 영숫자) MCP_NO 부분 입력은 match_product_tokens helper가 처리하므로 별도 pandas filter로 과도하게 분해하지 않는다.
같은 제품 표현 안에 F+숫자 lead token과 MCP_NO prefix처럼 서로 다른 역할의 구조화 token이 함께 있으면 둘 중 하나만 일반 filter로 만들고 나머지를 버리지 않는다. 모든 제품 token을 원문 순서대로 하나의 input_text에 보존하고 product_token_match를 선택한다. 예를 들어 `F315 L-116로 시작하는 제품`은 input_text=`F315 L-116`이며, F315와 L-116을 모두 만족하는 행만 helper가 찾게 한다.
match_product_tokens가 선택된 input_text의 일부 또는 전체를 MCP_NO·LEAD·DEVICE·DEVICE_DESC 등의 별도 eq/contains/starts_with 조회 filter로 다시 적용하지 않는다. 예를 들어 helper가 L-217을 처리한 뒤 MCP_NO == "L-217"을 추가하면 L-217K9B 같은 prefix 일치 행이 먼저 제거된다.
lead/ball suffix가 붙은 숫자 표현은 LEAD 제품 속성 token이다. 예: 152ball, 78Lead.
일반 pandas filter로 표현 가능해 보여도 사용자가 제품 식별 token으로 말한 값이면 product_token_match를 선택한다.
사용자가 DEVICE, 디바이스, device code처럼 DEVICE 컬럼을 직접 지칭하지 않는 한, 이 패턴의 제품 식별 token을 DEVICE filter로 만들지 않는다.
단, domain metadata에 등록된 제품군/제품 조건 alias는 product_token_match가 아니라 해당 domain 조건으로 처리한다. 예를 들어 POP제품, MOBILE/모바일 제품, HBM제품처럼 등록된 제품군을 부르는 경우에는 제품 token helper를 선택하지 않는다.
DA공정, D/A공정, WB공정, W/B공정, FCB공정, BG공정처럼 공정명 또는 공정 그룹만 말한 경우는 제품 token 매칭이 아니다.
공정 조건은 match_product_tokens에 넣지 말고 retrieval job의 filters 또는 pandas 전처리 조건으로 OPER_NAME에 적용한다. 여러 source를 비교하는 질문에서는 공정 조건을 질문 전체 목록으로 합치지 말고, 각 metric 표현이 수식하는 source_alias에만 적용한다.
예를 들어 `D/A1, D/A2공정`처럼 여러 세부 공정을 명시하면 `OPER_NAME in ["D/A1", "D/A2"]`로 전체 목록을 보존한다. 마지막 값에 `공정`, `에서`, `의` 같은 한글 표현이 붙어도 그 값을 누락하거나 첫 번째 공정만 남기지 않는다.
`WBM`, `W/BM`, `B/M`은 WB 또는 W/B 공정 그룹의 축약·부분 표현이 아니라 독립 공정 alias다. 이 표현이 질문에 있으면 metadata에 등록된 해당 독립 공정 값만 `OPER_NAME` 조건에 사용하고, WB 공정 그룹(`W/B1`~`W/B6`)을 선택하거나 그 목록으로 확장하지 않는다.
예를 들어 `오늘 WBM 공정의 제품별 생산량`과 `2026-07-01 W/BM 공정 생산량`은 `OPER_NAME in ["W/BM", "B/M"]` 범위로 해석한다. 제품별 질문은 표준 제품 grain으로 집계하고, 단순 공정 생산량 질문은 요청한 공정 범위의 생산량만 집계한다.
`DA1`, `WB2`, `DS1`처럼 slash를 생략한 숫자 세부 공정은 선택된 공정 그룹 metadata의 `question_match.processes`가 제공하는 등록값(`D/A1`, `W/B2`, `D/S1`)만 사용한다. 이는 세부 공정 canonical lookup 결과이므로 전체 공정 그룹으로 확대하지 않으며, 일치값이 없으면 임의로 만들지 않는다.
`DA1`, `WB2`, `DS1`처럼 slash를 생략한 숫자 세부 공정은 선택된 공정 그룹 metadata의 `payload.processes`에서 slash를 제거했을 때 유일하게 같은 값이 있는 경우에만 그 등록값(`D/A1`, `W/B2`, `D/S1`)으로 정규화한다. 일치하는 등록값이 없거나 여러 개이면 임의로 만들지 않는다.
같은 source 절에서 `D/S1 & D/A 공정`처럼 세부 공정 하나와 공정 그룹을 함께 요청하면 AND로 함께 요청한 두 범위를 모두 포함한다. 행 filter는 같은 OPER_NAME 컬럼에 서로 배타적인 AND 조건 두 개를 걸지 말고 `OPER_NAME in ["D/S1", "D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"]`처럼 합친다.
같은 source 절에서 `DA, WB공정`, `WB & DA 공정`, `DA와 WB공정`처럼 연결된 공정 그룹 별칭의 마지막에만 `공정`이 붙으면 연결된 모든 등록 그룹을 포함한다. 각각 `DA공정, WB공정`이라고 쓴 것과 같은 범위다.
반대로 `DA 16G, WB공정`처럼 별칭 사이에 제품 속성 token이 끼면 DA까지 공정 그룹으로 확장하지 않는다. `DA, WB HOLD LOT`처럼 질문 어디에도 `공정`이 없는 표현도 이 공유 접미사 규칙으로 차단하거나 clarification으로 바꾸지 않고 기존 intent 판단을 유지한다.
공정 목록 합집합 규칙은 하나의 metric/source 범위를 나열한 경우에만 적용한다. `INPUT 실적은 있으나 D/A공정 WIP가 없는 제품`처럼 metric마다 공정 역할이 다르면 production_today에는 `OPER_NAME=INPUT`, wip_today에는 D/A1~D/A6만 적용하고 INPUT과 D/A 목록을 양쪽 source에 합치지 않는다.

두 세부 공정을 `~`, `∼`, `～`, `부터 ... 까지`, `사이`, `구간`, `범위`로 이은 질문은 양 끝 공정만 고르는 조건이 아니라 순서 구간인지 먼저 확인한다.
예: `D/S1~D/A5`는 질문에 적힌 순서와 무관하게 두 label의 숫자 OPER_SEQ 최소값과 최대값 사이를 양 끝 포함해 조회하는 ordered range다.
구간 의미가 명확하고 source에 label/order 역할 컬럼이 있으면 intent_plan.pandas_function_cases에 key=`ordered_process_range`, function_name=`filter_ordered_range`, input_text=사용자의 두 끝점 표현, source_alias=대상 alias를 넣는다.
pandas_execution_plan에도 operation=`apply_pandas_function_case`, function_case_key=`ordered_process_range`, function_name=`filter_ordered_range`, 같은 input_text와 source_alias를 기록한다.
helper 호출 시 실제 schema의 label 컬럼과 order 컬럼을 각각 `label_column`, `order_column`으로 전달한다. 생산/재공 표준 schema에서는 `OPER_NAME`, `OPER_SEQ`를 사용한다.
양 끝점을 `OPER_NAME in [...]`로 바꾸거나 공정 그룹 전체 목록으로 펼치지 않는다. helper가 label에서 각 끝점의 order를 찾은 다음 숫자형 min/max 포함 범위를 적용하게 한다.
끝점이 source label에 없거나, 같은 정규화 label이 서로 다른 order로 연결되거나, label/order 컬럼이 없으면 값을 추측하지 않고 빈 결과로 닫는다.
`D/A1-W/B6`처럼 hyphen 양쪽이 실제 공정 label로 각각 확인될 때만 hyphen을 범위 구분자로 본다. `L-218` 같은 영문 1자리-숫자 3자리 MCP_NO token 내부 hyphen은 범위 기호가 아니므로 filter_ordered_range를 선택하지 않는다.
구분자를 생략해 두 실제 label을 붙여 쓴 표현도 metadata의 ordered range 규칙과 source label lookup으로 두 끝점이 유일하게 확인될 때만 선택한다.
filter_ordered_range가 선택된 source에서는 이 helper가 모든 일반 pandas row filter보다 항상 먼저 실행되어야 한다. HOLD, 상태, LOT, 제품 조건뿐 아니라 같은 source에 적용할 다른 일반 filter도 모두 helper 실행 뒤로 보낸다.
helper보다 뒤에 적용할 조건은 해당 `retrieval_jobs[].filters`와 `condition_resolution.effective_filters`에 넣지 않는다. 두 위치의 조건은 Executor가 생성 코드보다 먼저 적용하므로 범위 끝점 공정 행을 제거할 수 있다. 단, 데이터를 가져오는 데 필요한 catalog의 `required_params`는 이 규칙의 대상이 아니다.
`pandas_execution_plan`에서는 해당 source의 첫 실행 단계로 operation=`apply_pandas_function_case`, function_name=`filter_ordered_range`를 기록하고, 그 뒤 operation=`apply_filters` 단계에 후속 조건의 field, operator, value를 기록한다.
최종 JSON을 반환하기 전에 filter_ordered_range의 source_alias를 다시 확인한다. 같은 alias의 일반 조건이 `retrieval_jobs[].filters` 또는 `condition_resolution.effective_filters`에 남아 있으면 제거하고 helper 뒤의 `apply_filters` 단계로 옮긴다.
예를 들어 `D/S1~D/A4 공정 Hold 된 Lot ID` 질문은 HOLD_STAT=OnHold를 선행 filter에 넣지 않는다. 먼저 filter_ordered_range를 실행하고, 그 결과에 HOLD_STAT=OnHold를 적용한 뒤 LOT_ID를 선택한다.

특정 LOT_ID 없이 `현재 HOLD LOT`, `HOLD된 LOT 목록`, `공정별 HOLD LOT ID`처럼 현재 HOLD 대상의 목록이나 현황을 요청하면 lot_status를 선택하고 HOLD_STAT=OnHold 조건을 적용한다.
`HOLD 된 Lot ID 알려줘`처럼 이력·시간·코드·상세 사유를 요구하지 않고 현재 HOLD 대상 식별자만 묻는 표현도 lot_status 대상이다. 질문에 HOLD라는 단어가 있다는 이유만으로 hold_history를 선택하지 않는다.
hold_history는 LOT_ID가 필수인 이력 dataset이므로 특정 LOT_ID가 질문이나 선행 결과에 없을 때 required_params.LOT_ID를 빈 문자열로 만든 채 선택하지 않는다.
특정 LOT의 최근 HOLD 코드, HOLD 시각, 상세 사유 또는 HOLD 이력을 요청한 경우에만 해당 LOT_ID를 required_params로 전달하여 hold_history를 선택한다.
현재 HOLD LOT들을 먼저 찾은 뒤 각 LOT의 이력을 요청하면 lot_status에서 LOT_ID를 먼저 조회하고 그 결과의 LOT_ID들을 후속 hold_history 조회에 전달한다.

질문에 제품별과 DEVICE/디바이스/device가 함께 나오면 DEVICE만 단독으로 보여주지 않는다.
이 경우 결과 groupby/display 기준에는 DEVICE와 함께 제품 식별 속성도 포함한다.
권장 표시 순서는 TECH, DEN 또는 DENSITY, MODE, ORG, PKG1 또는 PKG_TYPE1, PKG2 또는 PKG_TYPE2, LEAD, MCP_NO, DEVICE, 요청 지표 순서다.
LLM 답변 JSON에 answer_sections.result_table.display_columns를 넣을 수 있으면 위 원본 컬럼명 순서를 사용한다.

장비 배정 정보와 UPH를 함께 요청하면 equipment_assign과 eqp_uph의 실제 공통 모델·공정·Recipe 문맥으로 결합한다.
장비 배정과 Recipe를 함께 언급했다는 이유만으로 UPH 조회를 추가하지 않는다. Recipe, RECIPE_ID, 장비 모델은 equipment_assign 안에서도 조회할 수 있는 장비 배정의 조합·표시 속성이며, 그 표현만으로는 UPH 지표를 요청한 것이 아니다.
현재 질문에 UPH, 시간당 생산량처럼 UPH 지표를 직접 요구하는 표현이 있을 때만 analysis_recipes의 equipment_assignment_uph_join을 선택하고 equipment_assign과 eqp_uph를 함께 조회한다.
UPH 지표를 요청하지 않고 배정 장비, 장비 모델, Recipe 또는 그 조합·대수·목록만 요청하면 equipment_assignment_uph_join을 선택하지 않고 equipment_assign 하나에서 답한다.
직전 제품 결과를 가리키며 `이 제품들`, `위 제품들`, `해당 제품들`의 장비 대수나 목록을 묻고 실제로 이전 제품 행별 대응 장비를 찾는 의도이면 `reference_mode=previous_result_rows`로 판단한다. 이때 이전 결과의 grain을 새로 추측하지 않고 `previous_result`의 모든 제품 행을 left 기준으로 유지하며, row-match된 equipment_assign을 정규화된 `match_columns`별로 집계한 뒤 결합한다.
장비 대수는 equipment_assign 전체의 단일 장비 수를 모든 제품에 붙이지 말고 `match_columns`별 `EQUIP_ID` 또는 실제 장비 ID 컬럼의 `nunique`로 계산한다. 장비가 없는 이전 제품도 결과에서 제거하지 않고 장비 대수 0으로 표시한다.
장비 LIST도 함께 요청하면 같은 제품 grain별로 장비 ID의 중복 없는 목록을 집계한다. 장비 대수와 LIST를 동시에 요청하면 한 `groupby_and_aggregate` 단계의 `aggregations`에 `EQUIP_ID/nunique/EQUIP_COUNT`와 `EQUIP_ID/collect_unique/EQUIP_LIST`를 함께 기록하고 두 결과 컬럼을 모두 반환한다. 장비 모델이나 Recipe는 사용자가 표시를 요청한 경우에만 결과 속성으로 사용하고 이전 제품을 찾는 매칭 key에는 추가하지 않는다.
lot단위 조건 없이 장비 목록이나 작업 장비에 대한 질문은 equipment_assign을 사용한다.
lot_status에 eqp_id가 있다는 이유만으로 장비 목록 질문을 lot_status로 처리하지 않는다.
UPH 상세 결과에는 source에 있는 장비 모델(EQUIP_MODEL), Recipe(RECIPE_ID), 공정(OPER_NAME 또는 OPER_NM)을 공통 필수 문맥으로 유지하고, UPH를 요청한 경우 UPH를 지표 컬럼으로 포함한다.
장비 목록도 요청한 경우에만 장비 ID(EQUIP_ID 또는 EQP_ID)를 포함한다. PRESS_CNT, MCP_NO 등 나머지 속성은 사용자가 직접 요청했거나 해당 분석에 실제로 필요한 경우에만 선택하며 기본 출력으로 강제하지 않는다.
UPH가 장비 모델 또는 Recipe에 따라 다르다고 설명할 예정이면 해당 모델·Recipe·공정 원본 컬럼을 결과에서 제거하지 않는다.
UPH는 평균형 비가산 지표다. 사용자가 grouping 없이 `제품별 UPH`만 요청하면 catalog의 기본 상세 문맥인 장비 모델·Recipe·공정별 개별 UPH를 유지하고, 차수별·장비 기종별처럼 grouping을 명시하면 그 그룹별 UPH 평균을 계산한다. UPH를 합산하지 않는다.
`장비 기종`, `기종`, `장비 모델`은 eqp_uph의 EQUIP_MODEL 차원을 뜻한다. `Recipe`, `레시피`는 RECIPE_ID 차원을 뜻한다.

제품 token 매칭이 필요하면 intent_plan.pandas_function_cases 배열에 아래 형식으로 선택 정보를 남긴다.
function_name은 match_product_tokens를 사용한다.
input_text에는 사용자가 말한 제품 속성 token 묶음만 넣고, 날짜/공정/수량 표현은 넣지 않는다.
한 제품 표현에 여러 구조화 token이 있으면 input_text에 모두 남긴다. 일부 token만 retrieval filter로 옮기고 다른 token을 누락하지 않는다.
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
`INPUT 실적은 있으나 D/A공정 WIP가 없는 제품`은 INPUT 실적을 제품별로 양수 집계한 결과를 left 기준으로 두고, D/A WIP를 제품별로 집계해 양수인 제품을 제외하는 presence 비교다. 단순 left join 후 모든 행을 반환하지 말고 D/A WIP가 없거나 합계가 0인 제품만 남긴다.

질문에 날짜가 없고 생산량, 생산실적, 투입, 재공수량을 현재 기준으로 묻는 경우 table catalog에 당일용 dataset이 있으면 production_today 또는 wip_today를 우선 사용한다.
이력 또는 업무 시점 Domain이 선택되면 그 후보의 `temporal_semantics.dataset_key`, `date_param`, `requested_date_offset_days`, `disallowed_dataset_keys`를 그대로 의도 계획에 반영하고, 질문 표현만 보고 별도의 날짜 규칙을 만들지 않는다.
질문 날짜는 `state_summary.request_context.date_mentions[].resolved_value`를 기준일로 사용한다. 실제 조회일은 선택된 Domain의 `requested_date_offset_days`를 적용해 계산하며, `previous_value`는 offset이 -1인 계약의 검산 힌트로만 사용한다.
BOH 같은 업무 표현도 위 공통 계약의 Domain alias일 뿐이며, intent 정규화기에 표현별 날짜 계산을 추가하지 않는다.
현시간 기준 재공 같은 현재 시점 표현도 선택된 Domain 또는 Table Catalog 계약으로 dataset을 결정한다.

metadata와 충돌하는 특화 지시는 적용하지 않는다.
table catalog의 required_params는 반드시 data catalog 기준으로만 채운다.
required_params가 아닌 공정/제품/상태 조건은 filters 또는 pandas function case로 남긴다.
