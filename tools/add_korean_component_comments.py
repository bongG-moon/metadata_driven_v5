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
    "metadata_qa_flow/01a_mongodb_domain_metadata_loader.py": "도메인 용어·별칭·공정 그룹처럼 질문 해석에 필요한 활성 도메인 문서를 읽습니다.",
    "metadata_qa_flow/01b_mongodb_table_catalog_loader.py": "데이터셋·source type·조회 파라미터·컬럼을 설명할 활성 테이블 카탈로그 문서를 읽습니다.",
    "metadata_qa_flow/01c_mongodb_main_filter_loader.py": "표준 필터 이름·별칭·연산자·값 형식을 설명할 활성 메인 필터 문서를 읽습니다.",
    "metadata_qa_flow/02_metadata_qa_context_builder.py": "질문 유형을 판정하고 비밀값을 제거한 뒤 도메인·테이블·필터 후보를 점수화·projection·바이트 제한해 QA 문맥을 만듭니다.",
    "metadata_qa_flow/03_metadata_qa_variables_builder.py": "QA LLM에 전달할 질문, 축약 메타데이터 문맥과 출력 스키마를 각각의 Message로 분리합니다.",
    "metadata_qa_flow/04_metadata_qa_response_normalizer.py": "LLM 응답을 정규화하고 authoritative context로 표와 source 참조를 보강해 결정론적 QA 결과를 만듭니다.",
    "metadata_qa_flow/05_metadata_qa_message_adapter.py": "QA 결과를 답변·표·SQL·관련 메타데이터·경고 순서의 Markdown Message 하나로 렌더링합니다.",
    "metadata_qa_flow/06_metadata_qa_api_response_builder.py": "최종 QA API 응답에서 큰 내부 context를 제거하고 구조화 data와 Message envelope을 제공합니다.",
    "route_flow/01_flow_api_message_caller.py": "Smart Router가 선택한 하위 Flow Run API에 원문 질문과 부모 세션을 한 번만 전달하고 최종 Message를 추출합니다.",
    "route_flow_v2/01_cached_named_run_flow_tool.py": "Flow 이름을 현재 ID로 해석해 그래프만 캐시하는 Agent 도구이며, 부모 세션과 단일 Chat Input/Output 계약을 유지합니다.",
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


def annotate_file(path: Path, *, check: bool = False) -> bool:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"UTF-8 BOM은 허용하지 않습니다: {path}")
    text = raw.decode("utf-8")
    if "\ufffd" in text:
        raise ValueError(f"대체 문자(U+FFFD)가 있습니다: {path}")
    if OVERVIEW_MARKER in text:
        return False
    tree = ast.parse(text, filename=str(path))
    info = _component_info(tree, path)
    relative = path.relative_to(COMPONENT_ROOT).as_posix()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    insertions = _insertions(tree, info)
    rendered: list[str] = _header(relative, info)
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
    args = parser.parse_args()

    paths = sorted(path for path in COMPONENT_ROOT.rglob("*.py") if "__pycache__" not in path.parts)
    changed = [path for path in paths if annotate_file(path, check=args.check)]
    if args.check and changed:
        for path in changed:
            print(f"[missing] {path.relative_to(ROOT).as_posix()}")
        return 1
    print(f"한글 설명 주석 확인: 전체 {len(paths)}개, {'누락' if args.check else '보강'} {len(changed)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
