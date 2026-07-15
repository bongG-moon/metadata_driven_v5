from __future__ import annotations

import argparse
import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components"
OVERVIEW_MARKER = "# 컴포넌트 개요:"
ENCODING_HEADER = "# -*- coding: utf-8 -*-"


# 파일 이름만으로는 알기 어려운 처리 의도를 설명한다. 이 내용은 각 Python 파일의
# 상단 설명 블록에 들어가며, JSON을 재생성하면 Langflow 코드 편집기에서도 보인다.
PROCESS_NOTES: dict[str, str] = {
    "data_analysis_flow/00_analysis_request_loader.py": "질문과 이전 상태를 읽고 세션 ID와 한국 기준일을 결정한 뒤 공통 분석 페이로드를 초기화합니다.",
    "data_analysis_flow/01a_mongodb_domain_metadata_loader.py": "활성 도메인 용어·별칭·공정 그룹을 MongoDB에서 읽어 원본 분석 페이로드에 덧붙입니다.",
    "data_analysis_flow/01b_mongodb_table_catalog_loader.py": "활성 테이블 카탈로그를 읽어 데이터셋별 조회 방식과 컬럼 정보를 분석 단계에 제공합니다.",
    "data_analysis_flow/01c_mongodb_main_variable_loader.py": "메인 필터 메타데이터를 읽어 질문의 공정·제품·기간 조건을 표준 필터로 해석할 수 있게 합니다.",
    "data_analysis_flow/01d_metadata_candidates_builder.py": "질문 토큰으로 도메인·테이블을 각각 점수화하고, 테이블 최소 후보와 전체 메인 필터를 보장한 뒤 바이트 제한에 맞게 압축합니다.",
    "data_analysis_flow/01e_followup_hint_builder.py": "이전 분석 상태를 작게 압축하고 날짜·지표·그룹 조건의 상속/변경 가능성을 후속 질문 힌트로 만듭니다.",
    "data_analysis_flow/02_intent_variables_builder.py": "의도 LLM에 필요한 질문·이전 상태·후보 메타데이터·출력 스키마만 각각의 Message로 분리합니다.",
    "data_analysis_flow/04_intent_plan_normalizer.py": "LLM JSON을 추출해 분석 범위, 조건 변경 내역, 조회 작업, pandas 단계와 후속 질문 전략을 표준 형태로 정규화합니다.",
    "data_analysis_flow/04a_trusted_retrieval_job_hydrator.py": "LLM이 제안한 데이터셋 키를 활성 카탈로그와 다시 대조해 신뢰할 수 있는 source 설정과 필수 파라미터만 복원합니다.",
    "data_analysis_flow/05_mongodb_result_loader.py": "이전 상태의 data_ref를 따라 저장된 분석 결과를 복원하고 source alias·columns·rows를 후속 분석용으로 재구성합니다.",
    "data_analysis_flow/05a_upstream_entity_parameter_binder.py": "명시적 상위 결과의 식별자를 신뢰 카탈로그 binding에 따라 다음 조회 작업 파라미터로 연결합니다.",
    "data_analysis_flow/06_retrieval_job_validator.py": "조회 작업의 데이터셋·source type·필수 설정을 검사하고 실행 가능한 작업과 검증 오류를 분리합니다.",
    "data_analysis_flow/07_retrieval_job_router.py": "단일 retrieval_mode를 적용해 작업을 dummy·Oracle·H API·Datalake·Goodocs 실행 포트로 나눕니다.",
    "data_analysis_flow/08_dummy_data_retriever.py": "실데이터 없이도 대표 질문을 검증할 수 있도록 데이터셋별 fixture에 날짜·제품·공정 조건을 동일한 규칙으로 적용합니다.",
    "data_analysis_flow/09_oracle_query_retriever.py": "카탈로그의 SQL 템플릿과 파라미터를 검증하고 Oracle 연결·조회·행 변환·오류 표준화를 한 경계에서 처리합니다.",
    "data_analysis_flow/10_h_api_retriever.py": "카탈로그 설정으로 HTTP 요청을 만들고 응답 경로에서 행을 추출해 공통 source result 형식으로 반환합니다.",
    "data_analysis_flow/11_datalake_retriever.py": "사내 Datalake 클라이언트를 동적으로 준비하고 SQL 결과의 다양한 반환 형식을 표준 rows로 변환합니다.",
    "data_analysis_flow/12_goodocs_retriever.py": "Goodocs 문서 또는 inline rows를 읽고 시스템 컬럼을 정리해 공통 source result 형식으로 반환합니다.",
    "data_analysis_flow/13_source_retrieval_merger.py": "각 조회 분기의 결과를 입력 순서대로 합치면서 warnings·errors·trace를 잃지 않고 하나의 페이로드로 만듭니다.",
    "data_analysis_flow/14_retrieval_payload_adapter.py": "전체 행은 pandas 실행용 runtime_sources에 두고 LLM에는 schema와 작은 preview만 전달해 토큰 사용량을 줄입니다.",
    "data_analysis_flow/15_pandas_variables_builder.py": "pandas 코드 LLM에 전달할 의도 계획, source schema/preview, 선택 helper와 출력 계약을 분리해 만듭니다.",
    "data_analysis_flow/15a_selected_helper_code_builder.py": "전체 helper 라이브러리에서 의도 계획이 선택한 함수 정의만 찾아 코드 생성 프롬프트에 전달합니다.",
    "data_analysis_flow/17_pandas_code_executor.py": "생성 코드를 AST로 검사하고 제한된 pandas/numpy 환경에서 실행하며, 실패하면 이전 코드와 오류를 포함해 LLM 복구를 최대 한 번 수행합니다.",
    "data_analysis_flow/18_answer_variables_builder.py": "최종 답변 LLM에 필요한 질문·결과 요약·적용 조건·근거·경고만 안전한 크기로 압축합니다.",
    "data_analysis_flow/20_answer_response_builder.py": "LLM 답변과 결정론적 분석 결과를 합쳐 answer sections, evidence, 현재 상태와 후속 상태를 구성합니다.",
    "data_analysis_flow/21_answer_message_adapter.py": "구조화 답변을 표·진단·조회 계획이 포함된 Markdown Message 하나로 렌더링합니다.",
    "data_analysis_flow/22_api_response_builder.py": "웹/API 소비자가 필요한 결과만 남기고 runtime source와 대용량 내부 필드를 제거한 응답 envelope을 만듭니다.",
    "data_analysis_flow/23_mongodb_result_store.py": "후속 질문에 필요한 분석 결과를 압축 저장하고 TTL과 data_ref를 관리한 뒤 원래 페이로드에 저장 상태를 기록합니다.",
    "data_analysis_flow/function_case_helper_code_input_example.py": "pandas executor가 선택적으로 주입하는 제품 토큰 helper 예시이며, 원본 DataFrame을 바꾸지 않고 필터 결과를 반환합니다.",
    "metadata_qa_flow/00_metadata_qa_request_loader.py": "메타데이터 질문과 이전 상태를 읽기 전용 QA 페이로드로 초기화하고 trace 영역을 준비합니다.",
    "metadata_qa_flow/01_mongodb_metadata_snapshot_loader.py": "한 MongoClient와 짧은 TTL cache로 도메인·테이블 카탈로그·메인 필터 snapshot을 함께 읽습니다.",
    "metadata_qa_flow/02_metadata_qa_context_builder.py": "질문 유형을 판정하고 비밀값을 제거한 뒤 도메인·테이블·필터 후보를 점수화·projection·바이트 제한해 QA 문맥을 만듭니다.",
    "metadata_qa_flow/03_metadata_qa_variables_builder.py": "QA LLM에 전달할 질문, 축약 메타데이터 문맥과 출력 스키마를 각각의 Message로 분리합니다.",
    "metadata_qa_flow/04_metadata_qa_response_normalizer.py": "LLM 응답을 정규화하고 authoritative context로 표와 source 참조를 보강해 결정론적 QA 결과를 만듭니다.",
    "metadata_qa_flow/05_metadata_qa_message_adapter.py": "QA 결과를 답변·표·SQL·관련 메타데이터·경고 순서의 Markdown Message 하나로 렌더링합니다.",
    "metadata_qa_flow/06_metadata_qa_api_response_builder.py": "최종 QA API 응답에서 큰 내부 context를 제거하고 구조화 data와 Message envelope을 제공합니다.",
    "route_flow/01_flow_api_message_caller.py": "Smart Router가 선택한 하위 Flow Run API에 원문 질문과 부모 세션을 한 번만 전달하고 최종 Message를 추출합니다.",
    "route_flow_v2/01_cached_named_run_flow_tool.py": "Flow 이름을 현재 ID로 해석하고 고정 question 인자를 현재 그래프의 단일 Chat Input으로 변환하는 Agent 도구입니다.",
    "route_flow_v3/01_orchestrated_named_run_flow_tool.py": "선택된 하위 Flow를 lazy 실행하고 result_ref 기반 연계 호출에 필요한 축약 결과 계약을 Agent에 반환합니다.",
    "route_flow_v4/00_workflow_plan_parser.py": "자연어 계획 모델 또는 화면 Registry의 정의를 최대 4단계 workflow.plan.v1 계약으로 검증하고 Loop 입력으로 변환합니다.",
    "route_flow_v4/01_sequential_step_executor.py": "기본 Loop의 현재 단계에 지정된 Tool 하나만 호출하고 축약 결과와 result_ref 의존 상태를 다음 반복에 전달합니다.",
    "route_flow_v4/02_final_context_builder.py": "Loop가 수집한 단계별 축약 결과를 마지막 기본 Language Model이 한 번 합성할 제한된 Context로 구성합니다.",
    "route_flow_v4/03_workflow_final_response_builder.py": "최종 모델 문장과 검증된 Workflow 실행 Context를 결합해 단일 Message와 terminal API 응답을 만듭니다.",
    "session_state_flow/00_mongodb_session_state_loader.py": "직접 전달된 상태를 우선 사용하고, 없으면 session ID로 MongoDB 상태를 읽어 runtime 데이터를 제거한 작은 이전 상태를 만듭니다.",
    "session_state_flow/01_mongodb_session_state_writer.py": "응답의 next state를 압축하고 turn count를 증가시켜 세션 문서를 replace-upsert한 뒤 저장 상태를 반환합니다.",
}


FUNCTION_NOTES: dict[str, str] = {
    "build_request": "사용자 입력과 이전 상태를 후속 노드가 공유할 표준 요청 dict로 변환합니다.",
    "build_variables": "LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.",
    "build_metadata_candidates": "질문과 세 종류의 메타데이터에서 관련 후보를 독립 정책으로 선택합니다.",
    "build_followup_hint": "현재 질문과 이전 상태를 비교해 상속·변경·제거 가능 조건을 힌트로 만듭니다.",
    "normalize_intent_plan": "LLM 의도 결과를 신뢰 가능한 실행 계획 계약으로 정규화합니다.",
    "hydrate_retrieval_jobs": "활성 카탈로그를 기준으로 조회 작업의 source 설정을 다시 구성합니다.",
    "load_previous_result": "저장된 이전 분석 결과를 찾아 후속 분석에서 재사용 가능한 source로 복원합니다.",
    "validate_retrieval_payload": "조회 작업별 필수 필드와 허용 source type을 검사합니다.",
    "route_retrieval_jobs": "검증된 조회 작업을 실행 모드와 source type별 최소 bundle로 나눕니다.",
    "retrieve_dummy_data": "테스트 fixture를 실제 조회 결과와 같은 source result 계약으로 반환합니다.",
    "retrieve_oracle_data": "Oracle SQL 작업을 실행하고 결과 행 또는 표준 오류를 반환합니다.",
    "h_api_retrieve": "HTTP API 작업을 실행하고 지정된 응답 경로에서 결과 행을 꺼냅니다.",
    "datalake_retrieve": "Datalake SQL 작업을 실행하고 결과 객체를 표준 행 목록으로 바꿉니다.",
    "goodocs_retrieve": "Goodocs 또는 inline 데이터를 읽어 분석용 표준 행 목록으로 바꿉니다.",
    "merge_source_retrieval_payloads": "여러 조회 분기에서 돌아온 source result와 trace를 하나로 병합합니다.",
    "build_retrieval_payload": "조회 행과 LLM용 요약을 분리하는 pandas 실행 직전 페이로드를 만듭니다.",
    "build_selected_helper_code": "선택된 function case에 해당하는 helper 함수 코드만 추출합니다.",
    "execute_pandas_code": "안전성 검사를 통과한 pandas 코드를 제한된 namespace에서 한 번 실행합니다.",
    "execute_pandas_with_repair": "최초 실행 실패 시 이전 코드와 오류를 전달해 최대 한 번 복구한 결과를 반환합니다.",
    "build_pandas_repair_prompt": "복구 LLM이 원인과 기존 코드를 함께 볼 수 있도록 수정 프롬프트를 조립합니다.",
    "build_answer_response": "LLM 문장과 분석 결과를 합쳐 최종 구조화 답변과 다음 상태를 만듭니다.",
    "build_message": "구조화 결과를 사용자가 읽을 수 있는 단일 Markdown Message로 변환합니다.",
    "build_api_response": "내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.",
    "store_result": "후속 질문 재사용에 필요한 결과를 MongoDB에 저장하고 data_ref를 발급합니다.",
    "load_existing_items": "등록 후보와 비교할 기존 MongoDB 문서를 최소 필드로 읽습니다.",
    "normalize_authoring": "LLM 등록 후보 JSON을 추출·검증해 저장 전 표준 items 배열로 정리합니다.",
    "check_similarity": "신규 후보와 기존 문서의 정확 key 또는 identity 충돌을 판정합니다.",
    "review_and_write": "결정론적 검증과 duplicate 정책을 적용하고 dry-run 계획 또는 실제 저장을 수행합니다.",
    "build_response": "저장 결과와 canonical target을 사용자 응답용 요약으로 바꿉니다.",
    "build_metadata_qa_context": "질문 유형에 맞는 안전하고 작은 메타데이터 근거 문맥을 구성합니다.",
    "normalize_metadata_qa_response": "LLM QA 결과를 근거 문맥과 결합해 안정적인 답변 계약으로 정규화합니다.",
    "run_flow_api_message": "선택된 하위 Flow API를 호출하고 중간 출력이 아닌 최종 Message만 반환합니다.",
    "load_session_state": "직접 상태 또는 MongoDB 세션 상태를 후속 질문용 크기로 정규화합니다.",
    "write_session_state": "현재 응답의 next state를 세션 문서에 원자적으로 갱신합니다.",
    "match_product_tokens": "질문의 제품 토큰을 표준 제품 컬럼에 역할별로 매칭해 DataFrame을 필터링합니다.",
    "sample_passthrough_helper": "여러 helper 선택 형식을 검증하기 위해 DataFrame 복사본을 그대로 반환합니다.",
    "ensure_package": "선택적 외부 패키지를 확인하고 허용된 경우에만 준비합니다.",
    "get_connection": "설정에서 Oracle 연결 객체를 만들고 호출자에게 반환합니다.",
    "execute_query": "Oracle cursor 실행 결과를 컬럼명 기반 dict 행으로 변환합니다.",
    "execute_sql": "Datalake 클라이언트에 SQL을 전달하고 원시 결과를 반환합니다.",
    "read_all": "Goodocs 클라이언트에서 문서의 전체 행을 읽습니다.",
    "get_graph": "대상 Flow 이름을 ID로 해석하고 재사용 가능한 그래프를 가져옵니다.",
    "get_new_fields": "대상 Flow의 실제 Chat Input만 Agent tool schema로 노출합니다.",
    "get_required_data": "Flow tool 실행에 필요한 그래프와 입력 정보를 준비합니다.",
    "update_build_config": "모델 선택에 따라 동적 입력 필드를 갱신하는 Langflow 빌드 lifecycle 함수입니다.",
    "replace": "현재 문서를 canonical 대상에 맞춰 교체 저장합니다.",
    "save_current": "새 문서를 현재 key로 저장합니다.",
}


# 여러 Flow에서 반복해서 쓰는 private helper는 이름만으로도 역할을 정확히 설명할 수 있다.
# 먼저 이 사전을 사용하고, 없는 함수는 아래의 동작/대상 token 규칙으로 설명을 만든다.
COMMON_FUNCTION_NOTES: dict[str, str] = {
    "__init__": "외부 클라이언트나 실행 설정을 인스턴스에 보관해 뒤의 메서드가 같은 연결 문맥을 사용하게 합니다.",
    "_payload": "Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.",
    "_dict": "입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.",
    "_list": "입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.",
    "_as_list": "단일 값과 여러 값 입력을 모두 같은 list 형태로 맞춰 반복 처리를 단순화합니다.",
    "_row_list": "여러 입력 형태에서 dict인 행만 골라 표준 행 목록으로 반환합니다.",
    "_items": "Langflow 값이나 payload에서 저장·비교 대상 items 목록만 안전하게 꺼냅니다.",
    "_int": "문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.",
    "_safe_int": "예외를 발생시키지 않고 값을 정수로 바꾸며 허용되지 않는 값은 기본값으로 처리합니다.",
    "_nonnegative_int": "입력값을 0 이상의 정수로 제한해 횟수·크기 설정에 음수가 들어가지 않게 합니다.",
    "_bool": "문자열·숫자·불리언 표기를 일관된 bool 값으로 해석합니다.",
    "_truthy": "입력값이 활성/참 의미로 해석되는지 공통 규칙으로 판정합니다.",
    "_text": "Message나 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.",
    "_text_value": "Langflow Message/Data에서 실제 문자열 값을 꺼내 공통 텍스트 형식으로 맞춥니다.",
    "_clean": "선택 입력을 안전한 문자열로 바꾸고 불필요한 앞뒤 공백을 제거합니다.",
    "_string_list": "여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.",
    "_json": "Message·dict·JSON 문자열에서 Markdown fence를 제거하고 JSON object를 안전하게 추출합니다.",
    "_json_dumps": "datetime·Decimal 같은 값까지 JSON-safe 형태로 바꾼 뒤 문자열로 직렬화합니다.",
    "_json_ready": "datetime·Decimal·NaN 등 JSON이 직접 표현하지 못하는 값을 안전한 기본형으로 재귀 변환합니다.",
    "_json_bytes": "현재 값을 UTF-8 JSON으로 직렬화했을 때의 실제 바이트 크기를 계산합니다.",
    "_dict_get_ci": "키의 대소문자 차이를 무시하고 dict에서 요청한 값을 찾습니다.",
    "_omit_empty": "dict에서 빈 문자열·빈 목록·None 항목을 제거해 전달 payload를 작게 유지합니다.",
    "_clip_text": "문자열을 허용 길이 안으로 자르되 비어 있는 값과 말줄임 표시를 일관되게 처리합니다.",
    "_truncate": "표시 또는 저장 한도를 넘는 텍스트를 안전하게 줄입니다.",
    "_compact_list": "목록의 개수와 각 항목 크기를 제한해 LLM·상태 payload가 과도하게 커지지 않게 합니다.",
    "_compact_write_result": "MongoDB 저장 결과에서 사용자 응답에 필요한 상태와 key 정보만 남깁니다.",
    "_columns_from_rows": "행 목록의 key 등장 순서를 유지하면서 결과 테이블의 컬럼 목록을 계산합니다.",
    "_rows_columns": "행 목록과 명시 컬럼을 함께 정규화해 표준 rows/columns 쌍을 만듭니다.",
    "_row_dict": "객체·매핑·튜플 형태의 한 행을 컬럼명이 있는 dict 행으로 변환합니다.",
    "_result": "현재 처리 상태·행·오류를 공통 source result 계약으로 묶습니다.",
    "_standard_result": "정상 조회 결과를 dataset/source alias와 rows가 포함된 공통 결과 구조로 만듭니다.",
    "_error_result": "예외 정보를 공통 errors 배열과 status가 포함된 실패 결과 구조로 만듭니다.",
    "_skipped": "설정이나 대상 작업이 없어 실행하지 않은 이유를 표준 skipped 결과로 남깁니다.",
    "_mark_skipped": "현재 작업 payload에 실행 생략 상태와 구체적인 사유를 기록합니다.",
    "_mark_error": "현재 작업 payload에 오류 상태와 정규화된 오류 정보를 기록합니다.",
    "_resolve_config": "노드 입력·환경변수·카탈로그 기본값의 우선순위로 실제 실행 설정을 확정합니다.",
    "_resolve_mongo_config": "컴포넌트 입력→환경변수→기본값 순서로 MongoDB database와 collection 설정을 확정합니다.",
    "_status_query": "active/all 선택에 맞는 MongoDB status 조회 조건을 만듭니다.",
    "_source_config": "조회 작업 또는 카탈로그에서 허용된 데이터 소스 설정만 dict로 꺼냅니다.",
    "_source_type": "조회 작업의 source type을 표준 소문자 식별자로 정규화합니다.",
    "_jobs_for_source": "전체 조회 작업 중 지정한 source type에 해당하는 작업만 골라냅니다.",
    "_job_params": "조회 작업의 params를 안전한 dict로 정리해 retriever에 전달합니다.",
    "_required_param_names": "카탈로그 설정에서 실행 전에 반드시 있어야 하는 파라미터 이름을 추출합니다.",
    "_missing_required_params": "필수 파라미터 중 실제 작업 값에 없는 항목을 찾아 오류 목록으로 반환합니다.",
    "_fetch_limit": "설정된 조회 제한을 안전한 정수 범위로 보정합니다.",
    "_render_template": "검증된 파라미터를 SQL·URL·본문 템플릿에 치환해 실제 요청 문자열을 만듭니다.",
    "_sql_literal": "SQL 템플릿 파라미터를 자료형에 맞는 안전한 literal 표현으로 변환합니다.",
    "_missing_candidates": "현재 전달된 기존 문서만으로 비교할 수 없어 추가 조회가 필요한 후보를 찾습니다.",
    "_load_candidates": "후보 key에 해당하는 기존 MongoDB 문서만 추가 조회해 비교 범위를 최소화합니다.",
    "_doc_id": "메타데이터 항목의 section/key 계약으로 canonical MongoDB 문서 ID를 계산합니다.",
    "_key": "메타데이터 항목에서 비교·표시에 사용할 논리 key를 안전하게 꺼냅니다.",
    "_normalize_key": "key의 대소문자·공백·구분자 차이를 제거해 비교 가능한 표준 식별자로 바꿉니다.",
    "_duplicate_action": "요청에 지정된 skip/merge/replace/create_new 중복 처리 정책을 안전한 기본값과 함께 해석합니다.",
    "_deep_merge": "중첩 dict를 재귀 병합하되 새 값이 지정된 필드만 기존 문서에 반영합니다.",
    "_secret_paths": "저장 후보 내부에서 password·token 등 비밀값으로 의심되는 필드 경로를 재귀 탐색합니다.",
    "_is_secret_key": "필드 이름이 credential·token·password 등 저장 금지 비밀 key인지 판정합니다.",
    "_redact_raw_text": "등록 원문에 포함될 수 있는 credential 값을 응답·trace에서 마스킹합니다.",
    "_unique_errors": "중복 오류 메시지를 최초 발생 순서대로 하나씩만 남깁니다.",
    "_merge_review": "결정론적 검증 결과와 선택적 추가 검수 결과를 하나의 저장 판단으로 합칩니다.",
    "_dry_run_result": "실제 DB를 변경하지 않고 실행 예정 작업만 보여 주는 dry-run 결과를 만듭니다.",
    "_write_to_mongodb": "검증을 통과한 작업만 duplicate action에 맞춰 MongoDB에 저장하고 결과를 기록합니다.",
    "_next_key": "create_new 정책에서 기존 key와 충돌하지 않는 다음 저장 key를 계산합니다.",
    "_markdown_table": "컬럼과 행을 길이 제한·escape 규칙이 적용된 Markdown 표로 렌더링합니다.",
    "_escape": "Markdown 표 셀을 깨뜨리는 구분자와 줄바꿈 문자를 안전하게 escape합니다.",
    "_escape_table_cell": "표 셀 안의 파이프·줄바꿈을 escape해 Markdown 열 구조가 깨지지 않게 합니다.",
    "_display_value": "None·숫자·복합 값을 사용자에게 읽기 좋은 짧은 문자열로 표시합니다.",
    "_notices": "warnings와 errors를 사용자에게 보여 줄 중복 없는 안내 목록으로 정리합니다.",
    "_key_points": "구조화 응답에서 사용자가 먼저 확인할 핵심 요약 문장을 추출합니다.",
    "_table_section": "구조화 rows/columns를 최종 답변의 표 section 계약으로 변환합니다.",
    "_status": "여러 단계의 실행 결과를 우선순위에 따라 최종 상태 문자열로 결정합니다.",
    "_summary": "현재 처리 결과의 건수·상태·핵심 정보를 짧은 요약 dict로 만듭니다.",
    "_next_steps": "현재 상태와 오류 여부에 맞는 사용자 다음 단계 안내를 구성합니다.",
    "_score": "질문 token과 후보 메타데이터의 일치 정도를 점수로 계산합니다.",
    "_tokens": "문자열을 비교 가능한 검색 token 목록으로 분리·정규화합니다.",
    "_sanitize_value": "복합 값에서 비밀 필드와 불필요한 내부 값을 제거하고 JSON-safe 형태로 바꿉니다.",
    "_korea_today": "현재 시각을 한국 시간 기준 YYYYMMDD 날짜 문자열로 반환합니다.",
    "_korea_timezone": "표준 zoneinfo를 우선 사용하고 불가능할 때만 고정 KST timezone을 반환합니다.",
    "_run_once": "같은 Langflow 노드 실행에서 하위 Flow API가 중복 호출되지 않도록 결과를 한 번만 계산해 재사용합니다.",
    "_message_result": "하위 Flow 호출 상태와 최종 Message를 Router 공통 결과 구조로 묶습니다.",
    "_extract_message_text": "Langflow Run API의 중첩 응답에서 최종 사용자 Message 텍스트를 우선순위대로 찾습니다.",
    "_session_id": "명시 입력과 부모 Message의 session 정보를 우선순위대로 해석합니다.",
    "_secret_text": "SecretStr 또는 일반 입력에서 노출 없이 실제 credential 문자열만 꺼냅니다.",
    "_guard_code": "생성된 pandas 코드 AST를 검사해 import·파일·네트워크·위험 builtin 사용을 차단합니다.",
    "_normalize_safe_imports": "허용된 pandas/numpy import 문만 제거하고 executor가 주입한 신뢰 namespace를 사용하게 합니다.",
    "_safe_import_name": "import 문이 정확히 허용된 pandas/numpy alias 형태인지 확인합니다.",
    "_safe_numpy_namespace": "허용 attribute만 노출하는 제한된 numpy namespace를 구성합니다.",
    "_invoke_repair_model": "기존 코드와 실제 오류가 포함된 프롬프트로 복구 모델을 정확히 한 번 호출합니다.",
    "_pandas_filter_preamble": "의도 계획의 필터 조건을 생성 코드보다 먼저 적용할 안전한 pandas 전처리 코드로 만듭니다.",
    "_condition_code": "단일 필터 조건을 pandas boolean mask 표현식으로 변환합니다.",
    "_analysis_error": "실행 예외를 type·message·짧은 traceback이 포함된 공개 가능한 오류로 정리합니다.",
    "record_step": "pandas 실행 중 단계별 DataFrame 크기와 설명을 trace에 기록합니다.",
    "record_function_case_result": "선택 helper 실행 결과의 함수명·입력·행 수를 분석 근거로 기록합니다.",
    "_norm": "제품 token 비교를 위해 값을 영문·숫자 중심의 표준 문자열로 정규화합니다.",
    "_lead_norm": "LEAD 값에서 Lead/Ball suffix를 제거해 숫자 역할 값으로 비교할 수 있게 합니다.",
    "_col_key": "컬럼명 표기 차이를 대문자 underscore key로 정규화합니다.",
    "_token_mask": "제품 token 하나를 역할별 컬럼 조건이 반영된 DataFrame boolean mask로 변환합니다.",
    "_looks_mcp_no_prefix": "입력 token이 영문 1자리-숫자 3자리 형태의 MCP_NO prefix인지 판정합니다.",
    "_has_rows": "pandas boolean mask에 실제로 선택된 행이 하나 이상 있는지 확인합니다.",
    "_lead_suffix_number": "Lead/Ball 단위 접미사가 붙은 표현에서 앞쪽 숫자만 추출합니다.",
    "_match": "선택한 제품 역할 컬럼들을 exact·contains·prefix 방식으로 OR 매칭합니다.",
    "_structured_search_values": "메타데이터 항목의 key·별칭·payload에서 질문 검색에 쓸 구조화 문자열을 재귀 수집합니다.",
    "_scalar_texts": "복합 입력 안의 문자열·숫자·불리언 값을 검색 가능한 문자열 목록으로 평탄화합니다.",
    "_stable_identity": "메타데이터 후보의 section과 key로 순서가 변하지 않는 중복 제거 식별자를 만듭니다.",
    "_annotate_runtime_function_cases": "선택 가능한 Function Case에 runtime 사용 가능 여부와 선택 근거를 덧붙입니다.",
    "_function_name": "Function Case 항목의 여러 호환 필드에서 실제 helper 함수 이름을 결정합니다.",
    "_token_variants": "질문 token의 구분자 제거·영숫자 결합 등 비교용 표기 변형을 만듭니다.",
    "_combined_status": "여러 MongoDB 로드 결과의 오류·성공·생략 상태를 하나의 최종 상태로 합칩니다.",
    "_list_limit": "QA 후보 최대 개수를 허용 범위 안의 정수로 보정합니다.",
    "_ranked": "메타데이터 항목을 질문 일치 점수와 원래 순서로 안정 정렬합니다.",
    "_candidate_rows": "QA 답변 모드에 맞춰 도메인·테이블·필터 후보를 공통 표 행으로 변환합니다.",
    "_source_refs": "선택된 메타데이터 후보의 section/key를 중복 없는 근거 참조 목록으로 만듭니다.",
    "_text_blob": "메타데이터의 주요 문자열 값을 하나의 검색용 텍스트로 합칩니다.",
    "_repair_function_case_selection": "복구 프롬프트에 전달할 선택 Function Case와 실행 단계만 작은 구조로 복사합니다.",
    "_pandas_execution_trace": "payload trace에서 기존 pandas 실행 기록을 안전한 dict로 꺼냅니다.",
    "_initial_failed_code": "실패 trace와 분석 결과에서 최초 생성 코드를 우선순위대로 복원합니다.",
    "_analysis_status": "분석 payload의 현재 pandas 실행 상태를 표준 문자열로 읽습니다.",
    "_analysis_error_value": "분석 payload에 기록된 실행 오류를 안전한 dict로 꺼냅니다.",
    "_result_to_rows": "DataFrame·list·dict·scalar 실행 결과를 rows와 columns 계약으로 변환합니다.",
    "_ordered_columns": "원본 컬럼 순서를 우선 유지하고 새 결과 컬럼을 뒤에 추가합니다.",
    "_scalar_result_row": "스칼라 pandas 결과를 지표명과 조건 문맥이 포함된 한 행 결과로 만듭니다.",
    "_scalar_context_row": "첫 조회 작업에서 날짜·공정·제품 조건을 스칼라 결과 표시 문맥으로 추출합니다.",
    "_filter_display_value": "필터의 단일/복수 값을 사람이 읽을 수 있는 짧은 표시값으로 변환합니다.",
    "_scalar_metric_label": "출력 계약과 질문을 바탕으로 스칼라 결과의 지표명을 결정합니다.",
    "_recorded_output": "pandas 단계 실행 결과를 행 수·컬럼·제한 preview가 포함된 trace 항목으로 만듭니다.",
    "_recorded_function_case": "Function Case 실행 결과를 함수명·입력·행 수·preview가 포함된 trace 항목으로 만듭니다.",
    "_preview_rows_columns_count": "대형 실행 결과에서 제한된 preview rows·columns·전체 행 수만 계산합니다.",
    "_runtime_helper_trace": "생성 코드가 실제 호출한 inline helper와 원본 정보를 실행 trace로 정리합니다.",
    "_used_inline_helpers": "생성 코드 AST에서 실제 호출된 helper 함수 이름만 찾아냅니다.",
    "_pandas_filter_plan": "조회 작업의 filter를 source alias별 결정론적 pandas 필터 계획으로 바꿉니다.",
    "_normalize_filter_operator": "필터 연산자의 여러 alias를 executor가 지원하는 표준 연산자로 바꿉니다.",
    "_null_empty_condition_lines": "null·not null·empty·not empty 조건에 해당하는 pandas mask 코드를 만듭니다.",
    "_has_operator_dict": "복합 필터 값이 operator를 가진 조건 dict인지 판정합니다.",
    "_column_choice_expression": "컬럼 alias 후보 중 실제 DataFrame에 존재하는 첫 컬럼을 선택하는 코드를 만듭니다.",
    "_filter_conditions": "dict/list 형태의 필터를 field·operator·values 조건 목록으로 정규화합니다.",
    "_as_values": "단일 필터 값과 목록 값을 같은 값 목록 형태로 맞춥니다.",
    "_field_candidates": "표준 필터 field에 대응할 수 있는 실제 컬럼 alias 후보를 반환합니다.",
    "_safe_name": "생성 코드에서 사용할 문자열을 안전한 Python 식별자 조각으로 정리합니다.",
    "_quantity_columns": "테이블 카탈로그 컬럼 중 수량·실적·계획 지표로 설명할 컬럼만 선별합니다.",
    "_fallback_payload": "LLM 응답이 비거나 잘못됐을 때 실제 metadata context로 결정론적 QA payload를 만듭니다.",
    "_service_table": "현재 metadata context를 사용 가능한 데이터 서비스 목록 표로 구성합니다.",
    "_usage_examples": "선택된 메타데이터 항목에 등록된 질문·사용 예시를 중복 없이 모읍니다.",
    "_route_hint": "현재 질문이 Metadata QA와 Data Analysis 중 어디로 가야 하는지 짧은 안내를 만듭니다.",
    "_sql_block": "선택한 테이블 카탈로그의 SQL 템플릿을 답변용 코드 블록 정보로 구성합니다.",
    "_output_schema": "LLM이 반드시 따라야 할 JSON 출력 필드와 자료형 계약을 만듭니다.",
    "_schema": "의도 분석 LLM이 반환해야 할 JSON 스키마를 작은 dict로 구성합니다.",
    "_previous_context": "이전 질문·의도·조건·결과 컬럼에서 후속 질문 판단에 필요한 문맥만 추출합니다.",
    "_explicit_date": "사용자 질문에 명시된 절대/상대 날짜 표현을 찾아 표준 날짜로 해석합니다.",
    "_notes": "후속 질문 해석에서 사용자에게 알릴 조건 상속·변경 주의사항을 구성합니다.",
    "_extend_unique": "대상 목록에 새 문자열을 중복 없이 원래 순서대로 추가합니다.",
    "_catalog_items": "MongoDB 로드 결과에서 active 테이블 카탈로그 항목만 안전하게 꺼냅니다.",
    "_issue": "조회 작업 hydration 중 발견한 문제를 type·dataset·message 구조로 만듭니다.",
    "_mode": "retrieval_mode 입력을 dummy/live 중 하나로 정규화합니다.",
    "_reuse_strategy": "의도 계획의 이전 결과 재사용 전략을 허용된 값으로 정규화합니다.",
    "_condition_resolution": "이전 조건의 inherited·changed·dropped·new 내역을 표준 구조로 정리합니다.",
    "_partial_intent_plan": "LLM 응답이 완전하지 않아도 복구 가능한 의도 계획 필드만 우선 추출합니다.",
    "_error": "조회 작업 검증 오류를 dataset·field·message가 포함된 표준 오류 dict로 만듭니다.",
    "_input_text": "Message/Data/JSON 입력에서 하위 Flow에 그대로 전달할 원문 질문을 추출합니다.",
    "_duration_ms": "시작 시각부터 현재까지의 API 호출 시간을 밀리초 정수로 계산합니다.",
    "_ref_id": "여러 data_ref 표현에서 실제 MongoDB 결과 참조 ID를 추출합니다.",
    "_data_ref_object": "문자열 또는 dict 참조를 ref_id 중심의 표준 data_ref 객체로 바꿉니다.",
    "_restore_data_from_stored_payload": "저장 payload의 rows·columns·source alias를 후속 분석용 data/runtime_sources로 복원합니다.",
    "_retrieval_mode": "payload와 작업 설정에서 실제 dummy/live 조회 모드를 결정합니다.",
    "_deterministic_review": "스키마·필수 필드·비밀값·중복 정책을 Python 규칙으로 검증해 저장 가능 여부를 결정합니다.",
    "_resolution_error": "canonical 대상이 없거나 여러 개인 경우 저장을 차단할 identity 오류를 만듭니다.",
    "_as_iso_text": "datetime 등 시간 값을 캐시 갱신 비교에 사용할 ISO 문자열로 변환합니다.",
    "_pre_run_setup": "명시 session_id가 없으면 부모 graph 세션을 상속하고 Flow tool 실행 전 상태를 준비합니다.",
    "_question_from_payload": "분석 payload의 request와 상태에서 현재 사용자 질문을 추출합니다.",
    "_connect_collection": "짧은 server selection timeout으로 MongoDB client와 대상 collection을 생성합니다.",
    "_collection_name": "입력·환경변수·기본값으로 실제 세션 상태 collection 이름을 결정합니다.",
    "_document_id": "session_id로 `session_state:{id}` 형식의 canonical 문서 ID를 만듭니다.",
    "_rows_from": "복합 데이터에서 저장/표시에 사용할 dict 행 목록을 추출합니다.",
    "_positive_int": "입력 숫자를 1 이상의 정수로 제한해 preview·history 한도에 사용합니다.",
    "_prepare_environment": "명시적으로 제공된 Datalake 인증·접속 설정만 실행 환경변수에 반영합니다.",
    "_rows_from_value": "외부 클라이언트의 DataFrame·list·dict 결과를 공통 dict 행 목록으로 변환합니다.",
    "_first_nested_list": "알려진 응답 key가 없을 때 중첩 dict에서 첫 번째 행 목록 후보를 찾습니다.",
    "_timeout": "HTTP timeout 입력을 허용 범위의 초 단위 숫자로 보정합니다.",
    "_safe_decode": "bytes 응답을 UTF-8로 해석하고 실패해도 예외 대신 읽을 수 있는 문자열을 반환합니다.",
    "_goodocs_class": "테스트 override가 있으면 우선 사용하고 아니면 기본 Goodocs 클라이언트 class를 가져옵니다.",
    "_selected_names": "Function Case 선택 JSON에서 실제로 요청된 helper 함수 이름만 순서대로 추출합니다.",
    "_rows_for_dataset": "dataset_key에 해당하는 dummy fixture 행을 복사해 조회 조건 적용 전 원본으로 제공합니다.",
    "_apply_params": "더미 행에 날짜·공정·제품 등 조회 파라미터 조건을 적용합니다.",
    "_apply_filters": "더미 행에 표준 field/operator/value 필터 조건을 순서대로 적용합니다.",
    "_oracle_config_from_value": "dict·JSON·TNS 텍스트 입력을 DB key별 Oracle 연결 설정으로 변환합니다.",
    "_deterministic_errors": "저장 후보의 필수 필드·허용값·비밀값 위반을 중복 없는 오류 목록으로 만듭니다.",
    "_keys": "저장 요청·operation에서 사용자에게 표시할 논리 key를 중복 없이 모읍니다.",
    "_helpers_from_selected_cases": "선택 Function Case 항목에서 pandas 프롬프트에 제공할 helper 이름만 추출합니다.",
    "_selected_function_cases": "의도 계획에서 실제 pandas 실행에 선택된 Function Case 항목만 정리합니다.",
    "_applied_criteria": "조회 작업과 pandas 계획에서 실제 적용된 날짜·제품·공정·지표 조건을 구성합니다.",
    "_metric_columns": "결과 컬럼 중 수량·실적·비율처럼 답변 지표로 사용할 컬럼을 선별합니다.",
    "_dimension_columns": "결과 컬럼 중 제품·공정·장비처럼 그룹 기준으로 사용할 컬럼을 선별합니다.",
    "_group_by_columns": "의도 계획의 pandas 단계에서 실제 그룹 기준 컬럼을 추출합니다.",
    "_readable_answer_text": "LLM 답변에서 불필요한 wrapper를 제거하고 사용자에게 표시할 본문만 남깁니다.",
    "_intent_decision_reasons": "의도 계획의 근거를 우선 사용하고 없으면 실행 계획에서 결정론적 근거를 만듭니다.",
    "_derived_korean_intent_reasons": "조회 데이터셋·조건·지표·그룹 정보를 자연스러운 한글 판단 근거로 변환합니다.",
    "_inspection": "payload trace에서 진단 표시용 inspection dict만 안전하게 꺼냅니다.",
    "_dict_value": "입력값이 dict인지 확인해 Message 렌더링 helper가 안전하게 key를 읽도록 합니다.",
    "_option_enabled": "메시지 표시 옵션의 문자열·불리언 값을 기본값과 함께 해석합니다.",
    "_json_text": "LLM 답변에서 Markdown fence를 제거하고 JSON object 문자열만 추출합니다.",
    "_evidence": "조회·pandas 실행 trace에서 답변 수치의 데이터셋과 조건 근거를 구성합니다.",
    "_zero_semantics_identity": "0값이 미집계인지 실제 0인지 구분하는 dummy 검증용 제품 식별자를 만듭니다.",
    "_production_processes_for_product": "지정 제품의 dummy 생산 실적이 존재하는 세부 공정 목록을 반환합니다.",
    "_wip_processes_for_product": "지정 제품의 dummy 재공이 존재하는 세부 공정 목록을 반환합니다.",
    "_process_index": "공정명을 정렬·fixture 계산에 사용할 안정적인 순번으로 변환합니다.",
    "_quantity_value": "제품·공정·일자 조합에서 재현 가능한 dummy 수량 값을 계산합니다.",
    "_oracledb": "테스트에서 주입된 Oracle driver를 우선 사용하고 없으면 실제 oracledb 모듈을 준비합니다.",
    "_config_has_values": "Oracle 설정 dict에 실제 접속값이 하나 이상 존재하는지 판정합니다.",
    "_downloads": "저장된 data_ref에서 최종 답변에 제공할 다운로드 항목을 구성합니다.",
    "_summary_basis": "답변 요약이 어떤 rows·지표·조건을 기준으로 작성됐는지 근거를 구성합니다.",
    "_current_data_state": "현재 결과의 rows·columns·row_count·data_ref를 다음 질문용 작은 상태로 만듭니다.",
    "_followup_source_results": "후속 질문이 재사용할 source result를 preview와 참조 중심으로 압축합니다.",
    "_runtime_source_refs": "메모리의 runtime source를 직접 저장하지 않고 재조회 가능한 source 참조만 구성합니다.",
    "_effective_ttl_hours": "요청값과 기본값을 해석해 결과 문서에 적용할 유효 TTL 시간을 결정합니다.",
    "_to_iso": "datetime 또는 문자열 시간을 UTC ISO 형식으로 변환합니다.",
    "_data_mode": "payload의 retrieval_mode와 source 결과를 확인해 dummy/live 응답 표시 모드를 결정합니다.",
}


TOKEN_LABELS: dict[str, str] = {
    "answer": "답변",
    "analysis": "분석",
    "api": "API",
    "applied": "적용 조건",
    "available": "사용 가능 항목",
    "candidate": "후보",
    "candidates": "후보",
    "case": "Function Case",
    "collection": "MongoDB 컬렉션",
    "column": "컬럼",
    "columns": "컬럼",
    "condition": "조건",
    "config": "설정",
    "context": "문맥",
    "criteria": "적용 기준",
    "current": "현재",
    "data": "데이터",
    "date": "날짜",
    "dataset": "데이터셋",
    "display": "표시값",
    "detail": "상세 정보",
    "download": "다운로드",
    "domain": "도메인",
    "duplicate": "중복 정책",
    "error": "오류",
    "errors": "오류",
    "filter": "필터",
    "filters": "필터",
    "function": "함수",
    "family": "데이터셋 분류",
    "for": "대상",
    "from": "원본",
    "group": "그룹",
    "identity": "식별자",
    "hint": "힌트",
    "intent": "의도 계획",
    "item": "항목",
    "items": "항목",
    "job": "조회 작업",
    "jobs": "조회 작업",
    "key": "key",
    "keys": "key",
    "label": "표시 라벨",
    "labels": "표시 라벨",
    "limit": "제한값",
    "logical": "논리 조건",
    "load": "조회 상태",
    "message": "Message",
    "metadata": "메타데이터",
    "mode": "실행 모드",
    "mongo": "MongoDB",
    "next": "다음 단계",
    "operation": "저장 작업",
    "operator": "연산자",
    "pandas": "pandas 실행",
    "param": "파라미터",
    "params": "파라미터",
    "payload": "페이로드",
    "preview": "미리보기",
    "previous": "이전 값",
    "points": "핵심 항목",
    "query": "쿼리",
    "ref": "참조",
    "refs": "참조",
    "request": "요청",
    "required": "필수 항목",
    "response": "응답",
    "result": "결과",
    "results": "결과",
    "retrieval": "데이터 조회",
    "route": "라우팅",
    "row": "행",
    "rows": "행 목록",
    "schema": "스키마",
    "scope": "분석 범위",
    "section": "응답 section",
    "sections": "응답 section",
    "secret": "비밀값",
    "session": "세션",
    "source": "데이터 소스",
    "sql": "SQL",
    "state": "상태",
    "status": "상태",
    "stored": "저장 데이터",
    "strategy": "재사용 전략",
    "summary": "요약",
    "table": "표",
    "text": "문자열",
    "token": "token",
    "tokens": "token",
    "type": "유형",
    "url": "URL",
    "usage": "사용 예시",
    "trace": "실행 추적",
    "value": "값",
    "values": "값",
    "warning": "경고",
    "warnings": "경고",
}


FUNCTION_COMMENT_MARKERS = ("# 주요 함수:", "# Langflow 출력 함수:", "# 주요 메서드:", "# 함수 설명:")


# 같은 함수명이라도 Flow에 따라 정책이 다른 경우에는 경로별 설명을 우선한다.
CONTEXT_FUNCTION_NOTES: dict[tuple[str, str], str] = {
    ("domain_saving_flow/05_domain_similarity_checker.py", "_identity_matches"): "같은 section 안에서 신규 항목의 key·alias·display name과 겹치는 기존 canonical 문서를 찾습니다.",
    ("domain_saving_flow/05_domain_similarity_checker.py", "_identity_parts"): "도메인 항목의 key·별칭·표시명을 identity 비교용 원문 조각으로 모읍니다.",
    ("domain_saving_flow/05_domain_similarity_checker.py", "_identity_token"): "NFKC·대소문자·구분자 정규화를 적용해 identity 비교 token을 만듭니다.",
    ("domain_saving_flow/05_domain_similarity_checker.py", "_identity_query"): "후보 identity와 겹칠 수 있는 기존 도메인 문서만 조회하는 MongoDB 조건을 만듭니다.",
    ("domain_saving_flow/05_domain_similarity_checker.py", "_conflict_warning"): "한 후보가 여러 기존 문서와 겹칠 때 ambiguous 저장 차단 경고를 구성합니다.",
    ("domain_saving_flow/07_domain_review_writer.py", "_resolve_match"): "similarity 결과에서 유일한 기존 canonical 문서만 merge/replace 대상으로 확정합니다.",
    ("domain_saving_flow/07_domain_review_writer.py", "_match_groups"): "신규 key별 similarity 결과를 묶어 유일·없음·모호함 상태를 계산합니다.",
    ("domain_saving_flow/07_domain_review_writer.py", "_canonical_identity"): "replace 후에도 유지해야 하는 기존 문서의 canonical section/key/_id를 결정합니다.",
    ("domain_saving_flow/07_domain_review_writer.py", "_canonical_key"): "저장 작업이 실제로 대상으로 삼는 canonical key를 계산합니다.",
    ("domain_saving_flow/07_domain_review_writer.py", "_operation_record"): "요청 key와 실제 target key를 함께 담은 저장 예정/완료 operation trace를 만듭니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_infer_answer_mode"): "질문 표현을 정의·소스 목록·SQL 설명·실데이터 redirect 등 QA 답변 모드로 분류합니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_select_domain_items"): "질문 token 점수로 관련 도메인 항목만 max_items 범위에서 선택합니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_select_table_items"): "질문과 답변 모드에 맞는 테이블 카탈로그 후보를 점수순으로 선택합니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_select_filter_items"): "메인 필터 후보를 질문 token과 별칭 일치 기준으로 선택합니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_project_domain_item"): "도메인 문서에서 QA 답변에 필요한 설명·별칭·공정 정보만 projection합니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_project_table_item"): "테이블 문서에서 dataset/source/컬럼/조회 설명만 안전하게 projection합니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_project_filter_item"): "메인 필터 문서에서 별칭·연산자·값 형식만 안전하게 projection합니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_fit_context_bytes"): "QA context가 max_bytes를 넘으면 낮은 우선순위 후보와 긴 문자열부터 단계적으로 줄입니다.",
    ("metadata_qa_flow/02_metadata_qa_context_builder.py", "_sanitize_value"): "LLM 문맥에서 trace·credential·내부 필드를 제거하고 비밀값을 마스킹합니다.",
    ("metadata_qa_flow/04_metadata_qa_response_normalizer.py", "_compact_answer_sections"): "큰 rows는 data.rows 한 곳에 두고 answer section에는 표시 정보만 남겨 중복 payload를 줄입니다.",
    ("metadata_qa_flow/04_metadata_qa_response_normalizer.py", "_should_use_context_table"): "현재 답변 모드에서 LLM 표 대신 authoritative metadata context 표를 써야 하는지 판정합니다.",
    ("metadata_qa_flow/04_metadata_qa_response_normalizer.py", "_fallback_answer"): "LLM 출력이 비어도 답변 모드와 실제 context에 근거한 결정론적 기본 답변을 만듭니다.",
    ("data_analysis_flow/17_pandas_code_executor.py", "_with_repair_trace"): "최초 코드·오류·수정 코드·재실행 결과를 한 번의 repair trace로 합칩니다.",
    ("data_analysis_flow/17_pandas_code_executor.py", "_safe_import_trace"): "허용 import 정규화 내역을 실행 근거에 남길 수 있는 작은 trace로 만듭니다.",
    ("data_analysis_flow/17_pandas_code_executor.py", "_with_pandas_filter_preamble"): "생성 코드 앞에 결정론적 필터 preamble을 한 번만 결합합니다.",
    ("data_analysis_flow/17_pandas_code_executor.py", "_compound_condition_lines"): "AND/OR 복합 필터 구조를 pandas mask 코드 여러 줄로 변환합니다.",
    ("data_analysis_flow/09_oracle_query_retriever.py", "save_current"): "현재까지 읽은 TNS alias와 여러 줄 설정을 완성된 설정 항목으로 저장하고 버퍼를 초기화합니다.",
    ("data_analysis_flow/09_oracle_query_retriever.py", "replace"): "Oracle SQL 템플릿 placeholder를 자료형에 맞는 SQL literal로 치환하고 누락 key를 기록합니다.",
    ("data_analysis_flow/10_h_api_retriever.py", "replace"): "HTTP URL·본문 템플릿 placeholder를 요청 파라미터 값으로 치환하고 누락 key를 기록합니다.",
    ("data_analysis_flow/11_datalake_retriever.py", "replace"): "Datalake SQL 템플릿 placeholder를 자료형에 맞는 SQL literal로 치환하고 누락 key를 기록합니다.",
    ("data_analysis_flow/21_answer_message_adapter.py", "_diagnostic_sections"): "표시 옵션이 켜진 경우에만 의도·조회·pandas 진단 section을 최종 Message에 추가합니다.",
    ("data_analysis_flow/21_answer_message_adapter.py", "_downloadable_data_refs"): "사용자가 내려받을 수 있는 저장 결과 data_ref만 중복 없이 선별합니다.",
    ("route_flow/01_flow_api_message_caller.py", "_extract_child_status"): "하위 Flow 응답의 status를 여러 envelope 형식에서 우선순위대로 추출합니다.",
    ("route_flow/01_flow_api_message_caller.py", "_timeout_value"): "호환 timeout 입력과 connect/read timeout을 requests가 요구하는 값으로 정규화합니다.",
    ("session_state_flow/00_mongodb_session_state_loader.py", "_compact_state"): "세션 상태에서 runtime source와 큰 rows를 제거하고 후속 질문에 필요한 요약만 남깁니다.",
    ("session_state_flow/01_mongodb_session_state_writer.py", "_state_from_response"): "분석 응답의 next_state/current_state에서 다음 턴에 저장할 상태를 우선순위대로 추출합니다.",
}


@dataclass(frozen=True)
class ComponentInfo:
    class_name: str
    display_name: str
    description: str
    inputs: tuple[tuple[str, str, bool], ...]
    outputs: tuple[tuple[str, str, str], ...]


def _literal_assignment(class_node: ast.ClassDef, name: str, default: Any = None) -> Any:
    for node in class_node.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        try:
            return ast.literal_eval(node.value)
        except Exception:
            return default
    return default


def _call_keyword(call: ast.Call, name: str, default: Any = "") -> Any:
    for keyword in call.keywords:
        if keyword.arg != name:
            continue
        try:
            return ast.literal_eval(keyword.value)
        except Exception:
            return default
    return default


def _ports(class_node: ast.ClassDef, assignment: str) -> list[ast.Call]:
    for node in class_node.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == assignment for target in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            return [item for item in node.value.elts if isinstance(item, ast.Call)]
    return []


def _component_info(tree: ast.Module, path: Path) -> ComponentInfo:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        display_name = _literal_assignment(node, "display_name", "")
        if not isinstance(display_name, str) or not display_name.strip():
            continue
        description = _literal_assignment(node, "description", "")
        inputs = tuple(
            (
                str(_call_keyword(call, "display_name") or _call_keyword(call, "name") or "입력"),
                str(_call_keyword(call, "name") or ""),
                bool(_call_keyword(call, "required", False)),
            )
            for call in _ports(node, "inputs")
        )
        outputs = tuple(
            (
                str(_call_keyword(call, "display_name") or _call_keyword(call, "name") or "출력"),
                str(_call_keyword(call, "name") or ""),
                str(_call_keyword(call, "method") or ""),
            )
            for call in _ports(node, "outputs")
        )
        return ComponentInfo(
            class_name=node.name,
            display_name=display_name.strip(),
            description=str(description or "이 Langflow 컴포넌트의 업무 단계를 처리합니다.").strip(),
            inputs=inputs,
            outputs=outputs,
        )

    relative = path.relative_to(COMPONENT_ROOT).as_posix()
    if relative.endswith("function_case_helper_code_input_example.py"):
        return ComponentInfo(
            class_name="",
            display_name="pandas Function Case Helper Library",
            description="선택된 pandas 분석에서만 주입하는 재사용 helper 함수 모음입니다.",
            inputs=(("사용자 표현", "input_text", True), ("원본 DataFrame", "frame", True)),
            outputs=(("필터링된 DataFrame", "result", ""),),
        )
    raise ValueError(f"Langflow 컴포넌트 정보를 찾을 수 없습니다: {relative}")


def _saving_process_note(relative: str) -> str:
    folder, stem = relative.split("/", 1)
    label = {
        "domain_saving_flow": "도메인",
        "table_catalog_saving_flow": "테이블 카탈로그",
        "main_flow_filters_saving_flow": "메인 플로우 필터",
    }.get(folder, "메타데이터")
    if "existing_items_loader" in stem:
        return f"{label} 등록 후보와 비교할 기존 문서를 MongoDB에서 최소 projection으로 읽고 registration trace 같은 불필요 필드를 제거합니다."
    if "saving_request_loader" in stem:
        return f"자연어 {label} 등록 요청을 duplicate action과 기본 dry-run이 포함된 안전한 표준 페이로드로 초기화합니다."
    if "saving_variables_builder" in stem:
        return f"정제된 원문을 우선해 {label} authoring LLM에 필요한 텍스트 하나만 전달합니다."
    if "saving_result_normalizer" in stem:
        return f"Markdown code fence를 제거하고 LLM JSON의 호환 key를 {label} 저장 스키마로 정규화합니다."
    if "similarity_checker" in stem:
        return f"{label} 후보와 기존 문서의 canonical key 또는 허용된 identity 충돌을 찾아 저장 정책 결정에 필요한 match 정보를 만듭니다."
    if "review_writer" in stem:
        return f"{label} 필수 필드·비밀값·중복 정책을 결정론적으로 검증하고 dry-run 계획 또는 MongoDB 저장을 수행합니다."
    if "saving_response_builder" in stem:
        return f"{label} 등록 상태와 요청 key/실제 canonical key를 사람이 확인하기 쉬운 구조화 응답으로 요약합니다."
    if "saving_message_adapter" in stem:
        return f"구조화된 {label} 저장 결과를 요약·대상 표·다음 단계가 포함된 Markdown Message 하나로 렌더링합니다."
    if "saving_api_response_builder" in stem:
        return f"{label} 저장 결과를 웹/API용 dict와 JSON Message 두 출력 계약으로 변환합니다."
    return f"{label} 저장 Flow의 현재 단계를 처리해 다음 노드가 사용할 표준 페이로드를 만듭니다."


def _process_note(relative: str, description: str) -> str:
    if relative in PROCESS_NOTES:
        return PROCESS_NOTES[relative]
    if "_saving_flow/" in relative or "filters_saving_flow/" in relative:
        return _saving_process_note(relative)
    return description


def _maintenance_note(relative: str) -> str:
    if relative.endswith("01d_metadata_candidates_builder.py"):
        return "도메인/테이블/메인 필터 quota는 서로 독립적이며, 테이블 최소 후보와 max_bytes 계약을 함께 지켜야 합니다."
    if relative.endswith("17_pandas_code_executor.py"):
        return "파일·네트워크 I/O와 임의 import는 차단하고 pandas/numpy alias만 허용합니다. 복구 호출은 실행 오류당 최대 한 번입니다."
    if relative.endswith("21_answer_message_adapter.py") or relative.endswith("05_metadata_qa_message_adapter.py") or "saving_message_adapter.py" in relative:
        return "이 노드만 최종 Chat Output에 연결해 중간 질문이나 JSON이 대화 기록에 중복 출력되지 않게 합니다."
    if relative.startswith("route_flow/"):
        return "Smart Router의 비선택 분기는 호출하지 않으며, 부모 session_id와 connect/read timeout을 하위 Flow에 일관되게 전달합니다."
    if relative.startswith("route_flow_v2/"):
        return "cache_flow는 그래프 빌드만 재사용하고 답변은 캐시하지 않습니다. return_direct와 부모 세션 상속 계약을 유지합니다."
    if relative.startswith("session_state_flow/"):
        return "세션 상태에는 runtime_sources와 대용량 rows를 그대로 넣지 말고 후속 질문에 필요한 요약과 data_ref만 남깁니다."
    if "mongodb" in relative or "existing_items_loader" in relative or relative.endswith("review_writer.py") or relative.endswith("result_store.py"):
        return "연결 설정은 노드 입력→환경변수→기본값 순으로 해석하며, 오류는 숨기지 않고 trace/status에 남기고 연결은 반드시 닫습니다."
    if relative.endswith("02_metadata_qa_context_builder.py"):
        return "secret/credential/raw trace를 문맥에 넣지 않고 max_items·max_bytes 제한을 넘으면 낮은 우선순위 후보부터 줄입니다."
    if relative.endswith("04_metadata_qa_response_normalizer.py"):
        return "표의 실제 rows와 source 참조는 메타데이터 context를 authoritative 근거로 사용하고 LLM 임의 값을 그대로 신뢰하지 않습니다."
    if relative.endswith("function_case_helper_code_input_example.py"):
        return "helper는 원본 DataFrame을 변경하지 않아야 하며, executor가 주입한 record_function_case_result가 있으면 실행 근거를 기록합니다."
    if "retriever.py" in relative:
        return "실행 오류를 다른 source의 성공처럼 위장하는 과도한 fallback은 만들지 말고 공통 errors 계약으로 전달합니다."
    if "saving_flow/" in relative or "filters_saving_flow/" in relative:
        return "LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다."
    return "inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다."


def _format_port(display_name: str, name: str, required: bool | None = None) -> str:
    suffix = " · 필수" if required else ""
    code = f" ({name})" if name else ""
    return f"{display_name}{code}{suffix}"


def _comment_lines(label: str, text: str, *, indent: str = "") -> list[str]:
    prefix = f"{indent}# {label}: "
    width = max(40, 116 - len(prefix))
    chunks = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False) or [""]
    lines = [prefix + chunks[0]]
    continuation = f"{indent}# " + " " * (len(label) + 2)
    lines.extend(continuation + chunk for chunk in chunks[1:])
    return lines


def _header(relative: str, info: ComponentInfo) -> list[str]:
    input_text = ", ".join(_format_port(*port) for port in info.inputs) or "별도 입력 없음"
    output_text = ", ".join(_format_port(display, name) for display, name, _method in info.outputs) or "일반 Python 반환값"
    lines = [
        ENCODING_HEADER,
        "# =============================================================================",
        f"{OVERVIEW_MARKER} {info.display_name}",
    ]
    lines.extend(_comment_lines("역할", info.description))
    lines.extend(_comment_lines("주요 입력", input_text))
    lines.extend(_comment_lines("주요 출력", output_text))
    lines.extend(_comment_lines("처리 흐름", _process_note(relative, info.description)))
    lines.extend(_comment_lines("유지보수 포인트", _maintenance_note(relative)))
    lines.extend(["# =============================================================================", ""])
    return lines


def _function_note(name: str, info: ComponentInfo) -> str:
    if name in FUNCTION_NOTES:
        return FUNCTION_NOTES[name]
    if name.startswith("load_"):
        return "외부 저장소의 필요한 항목을 읽어 현재 페이로드에 안전하게 합칩니다."
    if name.endswith("_jobs_out"):
        return "라우터가 분리한 해당 source type의 조회 작업 bundle을 출력합니다."
    if name.startswith("build_"):
        return f"{info.display_name} 단계의 결과를 다음 노드가 요구하는 출력 형태로 구성합니다."
    return f"{info.display_name} 단계에서 외부에 공개되는 주요 실행 지점을 담당합니다."


def _function_target(name: str) -> tuple[str, str]:
    tokens = [token for token in name.strip("_").split("_") if token]
    if not tokens:
        return "처리 대상", ""
    action = tokens[0]
    target_tokens = tokens[1:] or tokens
    translated: list[str] = []
    for token in target_tokens:
        label = TOKEN_LABELS.get(token, token.upper() if len(token) <= 4 else token)
        if label not in translated:
            translated.append(label)
    return "·".join(translated) or "처리 대상", action


def _internal_function_note(name: str, info: ComponentInfo, relative: str) -> str:
    context_note = CONTEXT_FUNCTION_NOTES.get((relative, name))
    if context_note:
        return context_note
    if name in COMMON_FUNCTION_NOTES:
        return COMMON_FUNCTION_NOTES[name]
    if name in FUNCTION_NOTES:
        return FUNCTION_NOTES[name]

    target, action = _function_target(name)
    if action in {"build", "create", "make", "compose"}:
        return f"{target} 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다."
    if action in {"compact", "prune"}:
        return f"{target}에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다."
    if action in {"normalize", "canonicalize"}:
        return f"{target}의 표기·자료형 차이를 비교와 저장에 사용할 표준 형태로 정규화합니다."
    if action in {"resolve", "choose"}:
        return f"여러 {target} 후보와 우선순위를 검토해 실제 사용할 값을 확정합니다."
    if action in {"extract", "parse"}:
        return f"복합 입력이나 응답에서 {target}을 찾아 검증 가능한 기본 Python 값으로 변환합니다."
    if action in {"load", "read", "get", "fetch"}:
        return f"입력 또는 외부 저장소에서 {target}을 읽고 호출자가 사용할 형태로 반환합니다."
    if action in {"select", "filter"}:
        return f"조건과 우선순위에 맞는 {target}만 골라 원래 순서를 유지해 반환합니다."
    if action in {"rank", "score", "count"}:
        return f"{target}의 일치도나 건수를 계산해 후보 비교와 요약에 사용합니다."
    if action in {"sanitize", "redact", "mask"}:
        return f"{target}에서 비밀값·내부 필드·직렬화 불가 값을 제거하거나 마스킹합니다."
    if action in {"merge", "combine", "append", "add"}:
        return f"여러 {target} 값을 순서와 중복 정책을 지키며 하나의 결과로 합칩니다."
    if action in {"dedupe", "unique"}:
        return f"{target}의 중복을 제거하고 최초 등장 순서를 유지합니다."
    if action in {"fit", "clip", "truncate", "bounded", "limit"}:
        return f"{target}이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다."
    if action in {"is", "has", "looks", "contains", "should"}:
        return f"입력값이 {target} 조건에 해당하는지 부작용 없이 bool로 판정합니다."
    if action in {"format", "render", "markdown", "escape", "display"}:
        return f"{target}을 Markdown 또는 사용자 화면에서 안전하게 읽을 수 있는 표현으로 변환합니다."
    if action in {"run", "execute", "retrieve", "call", "invoke"}:
        return f"{target} 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다."
    if action in {"write", "store", "save", "replace", "upsert"}:
        return f"검증된 {target}을 저장 정책에 맞게 기록하고 수행 결과를 trace에 남깁니다."
    if action in {"find", "match", "matched"}:
        return f"입력 조건과 일치하는 {target}을 찾아 비교·필터 결과로 반환합니다."
    if action in {"validate", "check", "guard", "ensure"}:
        return f"{target}이 실행·저장 계약을 만족하는지 검사하고 위반 내용을 명시적으로 반환합니다."
    if action in {"remove", "strip", "omit", "drop"}:
        return f"{target}에서 후속 단계에 불필요하거나 노출하면 안 되는 부분을 제거합니다."
    if action in {"split"}:
        return f"{target}을 의미 있는 단위로 나눠 개별 처리할 수 있는 목록으로 만듭니다."
    if action in {"project"}:
        return f"{target}에서 현재 질문과 응답에 필요한 허용 필드만 projection합니다."
    if action in {"infer"}:
        return f"입력 단서를 기준으로 {target}을 결정론적으로 추론합니다."
    if action in {"record", "mark"}:
        return f"{target}의 현재 상태와 근거를 후속 진단이 가능한 trace에 기록합니다."
    if "section" in name:
        return f"{target}을 최종 Message에 넣을 독립 Markdown section으로 렌더링합니다."
    if name.endswith("_row"):
        return f"{target}을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다."
    if name.endswith("_rows"):
        return f"{target}을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다."
    if name.endswith("_label"):
        return f"{target}의 내부 식별자를 사용자가 이해할 표시 라벨로 변환합니다."
    if name.endswith("_summary"):
        return f"{target}의 건수·조건·상태를 진단과 답변에 쓸 짧은 요약으로 만듭니다."
    if name.endswith("_status"):
        return f"여러 실행 결과를 확인해 {target}의 최종 상태를 결정합니다."
    if name.endswith("_url"):
        return f"{target}에 접근할 URL을 설정과 식별자로부터 안전하게 구성합니다."
    if action in {"structured", "scalar", "stable"}:
        return f"{target}을 후속 비교·표시에서 안정적으로 사용할 수 있는 값으로 구성합니다."
    if action in {"annotate"}:
        return f"{target}에 실행 단계와 선택 근거를 설명하는 trace 정보를 덧붙입니다."
    if action in {"source", "dataset", "domain", "filter", "candidate", "available"}:
        return f"{target} 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다."
    if action in {"state", "request", "response", "answer", "message", "result", "analysis"}:
        return f"{target}에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다."
    if action in {"function", "token", "date", "column", "columns", "criteria", "next", "pandas"}:
        return f"{target} 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다."
    if action in {"download", "operation", "identity", "table", "session", "status", "text", "list"}:
        return f"{target}을 현재 컴포넌트의 표준 반환 형태로 변환합니다."
    return f"{info.display_name} 처리 중 {target} 관련 값을 계산·변환하는 내부 helper입니다."


def _has_adjacent_function_comment(lines: list[str], line_number: int) -> bool:
    index = line_number - 2
    comment_lines: list[str] = []
    while index >= 0 and lines[index].lstrip().startswith("#"):
        comment_lines.append(lines[index].lstrip())
        index -= 1
    return any(marker in line for line in comment_lines for marker in FUNCTION_COMMENT_MARKERS)


def _add_missing_function_comments(
    tree: ast.Module,
    info: ComponentInfo,
    relative: str,
    lines: list[str],
    insertions: dict[int, list[str]],
) -> int:
    added = 0
    functions = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))),
        key=_decorated_start,
    )
    for node in functions:
        start = _decorated_start(node)
        planned = insertions.get(start, [])
        if any(marker in line for line in planned for marker in FUNCTION_COMMENT_MARKERS):
            continue
        if _has_adjacent_function_comment(lines, start):
            continue
        indent = " " * node.col_offset
        note = f"`{node.name}()`는 {_internal_function_note(node.name, info, relative)}"
        insertions.setdefault(start, []).extend(_comment_lines("함수 설명", note, indent=indent))
        added += 1
    return added


def _decorated_start(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    lines = [node.lineno]
    lines.extend(decorator.lineno for decorator in node.decorator_list)
    return min(lines)


def _insertions(tree: ast.Module, info: ComponentInfo) -> dict[int, list[str]]:
    insertions: dict[int, list[str]] = {}
    output_by_method = {method: (display, name) for display, name, method in info.outputs if method}

    def add(line_number: int, lines: list[str]) -> None:
        insertions.setdefault(line_number, []).extend(lines)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            note = _function_note(node.name, info)
            add(
                _decorated_start(node),
                [
                    f"# 주요 함수: {note}",
                    "# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.",
                ],
            )
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name == info.class_name:
            class_lines = [
                "# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.",
                "# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.",
            ]
        else:
            class_lines = [
                "# 내부 연동 도우미 클래스: 외부 라이브러리나 클라이언트 차이를 이 파일의 표준 호출 형태로 감쌉니다.",
            ]
        add(_decorated_start(node), class_lines)

        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name.startswith("_"):
                continue
            indent = " " * method.col_offset
            if method.name in output_by_method:
                display, output_name = output_by_method[method.name]
                lines = [
                    f"{indent}# Langflow 출력 함수: '{display} ({output_name})' 포트가 요청될 때 실행됩니다.",
                    f"{indent}# 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.",
                ]
            else:
                lines = [
                    f"{indent}# 주요 메서드: {_function_note(method.name, info)}",
                    f"{indent}# Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.",
                ]
            add(_decorated_start(method), lines)
    return insertions


def _strip_generated_function_comments(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    tree = ast.parse(text)
    remove_indexes: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start_index = _decorated_start(node) - 1
        index = start_index - 1
        block_indexes: list[int] = []
        while index >= 0 and lines[index].lstrip().startswith("#"):
            block_indexes.append(index)
            index -= 1
        marker_indexes = [line_index for line_index in block_indexes if "# 함수 설명:" in lines[line_index]]
        if not marker_indexes:
            continue
        first_generated = min(marker_indexes)
        for line_index in range(first_generated, start_index):
            if lines[line_index].lstrip().startswith("#"):
                remove_indexes.add(line_index)
    return "\n".join(line for index, line in enumerate(lines) if index not in remove_indexes).rstrip() + "\n"


def annotate_file(path: Path, *, check: bool = False, refresh_functions: bool = False) -> bool:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"UTF-8 BOM은 허용하지 않습니다: {path}")
    original_text = raw.decode("utf-8")
    text = _strip_generated_function_comments(original_text) if refresh_functions else original_text
    if "\ufffd" in text:
        raise ValueError(f"대체 문자(U+FFFD)가 있습니다: {path}")
    tree = ast.parse(text, filename=str(path))
    info = _component_info(tree, path)
    relative = path.relative_to(COMPONENT_ROOT).as_posix()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    has_header = OVERVIEW_MARKER in text
    insertions = {} if has_header else _insertions(tree, info)
    added_functions = _add_missing_function_comments(tree, info, relative, lines, insertions)
    refreshed = text != original_text.replace("\r\n", "\n").replace("\r", "\n")
    if has_header and added_functions == 0 and not refreshed:
        return False
    rendered: list[str] = [] if has_header else _header(relative, info)
    for line_number, line in enumerate(lines, start=1):
        rendered.extend(insertions.get(line_number, []))
        rendered.append(line)
    result = "\n".join(rendered).rstrip() + "\n"
    ast.parse(result, filename=str(path))
    if not check:
        path.write_text(result, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Langflow Python 원본에 초보자용 한글 설명 주석을 일관되게 추가합니다.")
    parser.add_argument("--check", action="store_true", help="수정하지 않고 주석 누락 파일이 있는지만 확인합니다.")
    parser.add_argument("--refresh-functions", action="store_true", help="자동 생성한 함수별 설명을 최신 설명 규칙으로 다시 작성합니다.")
    args = parser.parse_args()
    if args.check and args.refresh_functions:
        parser.error("--check와 --refresh-functions는 함께 사용할 수 없습니다.")

    paths = sorted(path for path in COMPONENT_ROOT.rglob("*.py") if "__pycache__" not in path.parts)
    changed = [
        path
        for path in paths
        if annotate_file(path, check=args.check, refresh_functions=args.refresh_functions)
    ]
    if args.check and changed:
        for path in changed:
            print(f"[missing] {path.relative_to(ROOT).as_posix()}")
        return 1
    print(f"한글 설명 주석 확인: 전체 {len(paths)}개, {'누락' if args.check else '보강'} {len(changed)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
