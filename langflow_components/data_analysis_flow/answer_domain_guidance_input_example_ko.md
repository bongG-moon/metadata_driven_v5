제품 token 매칭 결과가 제공된 경우, 사용자가 입력한 제품 표현이 어떤 제품/DEVICE/속성으로 매핑되었는지 한 문장으로 설명한다.
장비 ASSIGN 분석에서는 먼저 기준 제품과 기준 재공 수량을 설명하고, 이후 세부 공정별 ASSIGN 대수를 요약한다.
단계형 분석에서는 최종 결과만 말하지 말고, 기준이 된 중간 결과와 그 기준으로 계산한 최종 결과를 연결해서 설명한다.
도메인 특화 지침은 제공된 answer_context_json과 결과 데이터에 근거가 있을 때만 반영한다.

결과 테이블 표시명이 필요한 경우에만 answer_sections.result_table.column_labels에 아래 표시명을 넣는다.
- OPER_NAME, OPER_NM: 공정
- TOTAL_PRODUCTION, PRODUCTION, production_sum: 생산량
- PKG_OUT_QTY: PKG OUT 실적
- INPUT_QTY, input_sum: 투입수량
- TOTAL_WIP, WIP: 재공수량
- BOH_WIP: 아침재공
- WIP_PER_INPUT: 투입 대비 WIP
- ASSIGN_QTY, ASSIGN_COUNT: ASSIGN 대수
- wip_sum: WIP 합계
- MCP_NO: MCP NO
- WORK_DATE, WORK_DT: 기준일

결과 테이블 표시 순서가 필요한 경우에만 answer_sections.result_table.display_columns에 원본 컬럼명 기준으로 순서를 넣는다.
권장 순서: WORK_DATE, WORK_DT, OPER_NAME, OPER_NM, TECH, DEN, DENSITY, MODE, ORG, PKG1, PKG_TYPE1, PKG2, PKG_TYPE2, LEAD, MCP_NO, DEVICE, TOTAL_PRODUCTION, PRODUCTION, production_sum, PKG_OUT_QTY, INPUT_QTY, input_sum, BOH_WIP, TOTAL_WIP, WIP, WIP_PER_INPUT, ASSIGN_QTY, ASSIGN_COUNT, wip_sum
