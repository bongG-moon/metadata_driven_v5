"""Shared Flow-JSON helpers for the supported V2 builders.

The former V1 Data Analysis export is retired.  ``build_data_analysis_flow_v2``
and ``build_data_analysis_flow_v2_continuation`` still import the layout and
template helpers in this module, but this module no longer creates a V1 Flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "tools" / "assets" / "data_analysis_flow_v2_donor.json"
DEFAULT_TARGET = ROOT / "flow_exports" / "data_analysis_flow_v2_standalone.json"
REPAIR_PROMPT_SOURCE = ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md"
HELPER_LIBRARY_SOURCE = ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py"
REPAIR_PROMPT_NODE_ID = "TextInput-v5RepairPrompt"
LANGUAGE_MODEL_NODE_IDS = {
    "Agent-mevnw": "LanguageModel-intent",
    "Agent-SRcFc": "LanguageModel-pandas",
    "Agent-ynb4D": "LanguageModel-answer",
}
LANGUAGE_MODEL_SYSTEM_MESSAGES = {
    "LanguageModel-intent": "Follow the supplied prompt exactly and return only the requested JSON object.",
    "LanguageModel-pandas": 'Follow the supplied prompt exactly and return one JSON object with a non-empty "code" field. Do not return markdown fences or explanatory text.',
    "LanguageModel-answer": "Follow the supplied prompt exactly and return only the requested answer text.",
}
MONGO_GLOBAL_VARIABLE = "MONGO_URL"
TARGET_LANGFLOW_VERSION = "1.9.2"
TARGET_LANGUAGE_MODEL_SOURCE = ROOT / "tools" / "assets" / "langflow_1_9_2_language_model.py"
# Keep the default provider selection in one build-time contract.  Individual
# flow exports may still expose the provider's other model choices, but every
# generated LLM node starts with this model selected.
DEFAULT_LANGUAGE_MODEL = "gemini-3.5-flash-lite"
DATA_ANALYSIS_NOTE_PREFIX = "note-data-analysis-"

# The canvas uses the same native component dimensions as Langflow 1.9.2.
# A 450 px horizontal step and a 500 px branch step leave enough room for
# expanded component cards without baking presentation concerns into runtime
# component code.
DATA_ANALYSIS_V5_LAYOUT = {
    "ChatInput-Xs7uo": (-2400.0, 100.0),
    "CustomComponent-Fti0r": (-1500.0, -350.0),
    "CustomComponent-xpbhS": (-1500.0, 550.0),
    "CustomComponent-HFsYn": (-1050.0, 100.0),
    "CustomComponent-i0jbh": (-1050.0, 800.0),
    "MongoDBDomainMetadataLoader-OM3Hg": (-1050.0, 1300.0),
    "CustomComponent-kzlcF": (-1050.0, 1800.0),
    "CustomComponent-DXrpf": (-600.0, 1100.0),
    "CustomComponent-B1hbh": (-600.0, 100.0),
    "TextInput-GRnAm": (-600.0, 600.0),
    "Prompt Template-AUpQz": (-150.0, 100.0),
    "LanguageModel-intent": (300.0, 100.0),
    "CustomComponent-5o0CN": (750.0, 100.0),
    "CustomComponent-v5Hydrate": (1200.0, 100.0),
    "CustomComponent-O8vfz": (1650.0, 100.0),
    "CustomComponent-v5UpstreamBinder": (2100.0, 100.0),
    "CustomComponent-vVkhs": (2550.0, 100.0),
    "CustomComponent-x6NXu": (3000.0, 100.0),
    "CustomComponent-Pp7d0": (3450.0, -650.0),
    "CustomComponent-v5Oracle": (3450.0, -150.0),
    "CustomComponent-v5HApi": (3450.0, 350.0),
    "CustomComponent-v5Datalake": (3450.0, 850.0),
    "CustomComponent-v5Goodocs": (3450.0, 1350.0),
    "MongoDBDomainMetadataLoader-geCh1": (3900.0, 100.0),
    "CustomComponent-bhiAG": (4350.0, 100.0),
    "CustomComponent-v5ExecutionGate": (4800.0, 100.0),
    "CustomComponent-fc0Vb": (5250.0, 100.0),
    "Prompt Template-xtzD5": (5700.0, 100.0),
    "CustomComponent-v5Helper": (5700.0, 650.0),
    "TextInput-AXG9a": (5250.0, 1100.0),
    "LanguageModel-pandas": (6150.0, 100.0),
    "TextInput-v5RepairPrompt": (6150.0, 700.0),
    "CustomComponent-s3mf1": (6600.0, 100.0),
    "CustomComponent-AUrFb": (7050.0, 100.0),
    "CustomComponent-aKrkH": (7500.0, 100.0),
    "TextInput-VFbHh": (7500.0, 700.0),
    "Prompt Template-ELVKc": (7950.0, 100.0),
    "LanguageModel-answer": (8400.0, 100.0),
    "CustomComponent-BVItv": (8850.0, 100.0),
    "CustomComponent-fXdS4": (9300.0, 100.0),
    "CustomComponent-v5RuntimeCleanup": (9750.0, 100.0),
    "CustomComponent-A5y0b": (10200.0, 100.0),
    "ChatOutput-rwbTs": (11100.0, 100.0),
    "CustomComponent-3eVde": (10650.0, 700.0),
}

DATA_ANALYSIS_V2_LAYOUT_OVERRIDES = {
    "CustomComponent-v2FastResolver": (5250.0, 100.0),
    "CustomComponent-fc0Vb": (5700.0, 100.0),
    "Prompt Template-xtzD5": (6150.0, 100.0),
    "CustomComponent-v5Helper": (6150.0, 650.0),
    "TextInput-AXG9a": (5700.0, 1100.0),
    "TextInput-v5RepairPrompt": (6600.0, 700.0),
    "CustomComponent-s3mf1": (6600.0, 100.0),
    "CustomComponent-AUrFb": (7050.0, 100.0),
    "CustomComponent-aKrkH": (7500.0, 100.0),
    "TextInput-VFbHh": (7500.0, 700.0),
    "CustomComponent-BVItv": (7950.0, 100.0),
    "CustomComponent-fXdS4": (8400.0, 100.0),
    "CustomComponent-v5RuntimeCleanup": (8850.0, 100.0),
    "CustomComponent-A5y0b": (9300.0, 100.0),
    "ChatOutput-rwbTs": (10200.0, 100.0),
    "CustomComponent-3eVde": (9750.0, 700.0),
}

COMPONENT_FILES = {
    "CustomComponent-xpbhS": "data_analysis_flow/00_analysis_request_loader.py",
    "CustomComponent-i0jbh": "data_analysis_flow/01a_mongodb_domain_metadata_loader.py",
    "MongoDBDomainMetadataLoader-OM3Hg": "data_analysis_flow/01b_mongodb_table_catalog_loader.py",
    "CustomComponent-kzlcF": "data_analysis_flow/01c_mongodb_main_variable_loader.py",
    "CustomComponent-DXrpf": "data_analysis_flow/01d_metadata_candidates_builder.py",
    "CustomComponent-HFsYn": "data_analysis_flow/01e_followup_hint_builder.py",
    "CustomComponent-O8vfz": "data_analysis_flow/05_mongodb_result_loader.py",
    "CustomComponent-vVkhs": "data_analysis_flow/06_retrieval_job_validator.py",
    "CustomComponent-x6NXu": "data_analysis_flow/07_retrieval_job_router.py",
    "CustomComponent-Pp7d0": "data_analysis_flow/08_dummy_data_retriever.py",
    "MongoDBDomainMetadataLoader-geCh1": "data_analysis_flow/13_source_retrieval_merger.py",
    "CustomComponent-bhiAG": "data_analysis_flow/14_retrieval_payload_adapter.py",
    "CustomComponent-3eVde": "data_analysis_flow/22_api_response_builder.py",
    "CustomComponent-AUrFb": "data_analysis_flow/23_mongodb_result_store.py",
    "CustomComponent-Fti0r": "session_state_flow/00_mongodb_session_state_loader.py",
    "CustomComponent-fXdS4": "session_state_flow/01_mongodb_session_state_writer.py",
}

PROMPT_FILES = {
    "Prompt Template-AUpQz": "03_intent_prompt_template_ko.md",
    "Prompt Template-xtzD5": "16_pandas_prompt_template_ko.md",
    "Prompt Template-ELVKc": "19_answer_prompt_template_ko.md",
}
TEXT_INPUT_FILES = {
    "TextInput-GRnAm": "specialized_prompt_input_example_ko.md",
    "TextInput-VFbHh": "answer_domain_guidance_input_example_ko.md",
}

REMOVED_REPAIR_NODES = {
    "CustomComponent-ZUhxo",
    "Prompt Template-ej9jd",
    "Agent-nSPco",
    "PandasCodeExecutor-kRbBG",
    "CustomComponent-QJwmh",
}

NEW_COMPONENTS = {
    "CustomComponent-v5Hydrate": {
        "file": "data_analysis_flow/04a_trusted_retrieval_job_hydrator.py",
        "position": {"x": 1900.0, "y": 720.0},
        "inputs": [
            ("data", "payload", "의도 페이로드", True, None),
            ("data", "table_catalog_items", "전체 테이블 카탈로그", True, None),
            ("dropdown", "retrieval_mode", "데이터 조회 모드", False, "dummy"),
        ],
        "outputs": [("Data", "payload_out", "신뢰 조회 작업 페이로드", "build_payload")],
    },
    "CustomComponent-v5UpstreamBinder": {
        "file": "data_analysis_flow/05a_upstream_entity_parameter_binder.py",
        "position": {"x": 2290.0, "y": 720.0},
        "inputs": [("data", "payload", "상위 결과 복원 페이로드", True, None)],
        "outputs": [("Data", "payload_out", "상위 엔터티 바인딩 페이로드", "build_payload")],
    },
    "CustomComponent-v5Helper": {
        "file": "data_analysis_flow/15a_selected_helper_code_builder.py",
        "position": {"x": -1420.0, "y": 2290.0},
        "inputs": [
            ("message", "function_case_selection_json", "Function Case 선택 JSON", True, ""),
            ("message", "helper_library", "전체 helper library", False, ""),
        ],
        "outputs": [("Message", "selected_helper_code", "선택 helper 코드", "build_code")],
    },
    "CustomComponent-v5ExecutionGate": {
        "file": "data_analysis_flow/14a_retrieval_execution_gate.py",
        "position": {"x": 1690.0, "y": 1510.0},
        "inputs": [("data", "payload", "조회 페이로드", True, None)],
        "outputs": [("Data", "payload_out", "실행 제어 페이로드", "build_payload")],
    },
    "CustomComponent-v5Oracle": {
        "file": "data_analysis_flow/09_oracle_query_retriever.py",
        "position": {"x": 470.0, "y": 1510.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "oracle_config", "Oracle 설정/TNS", False, ""),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
    "CustomComponent-v5HApi": {
        "file": "data_analysis_flow/10_h_api_retriever.py",
        "position": {"x": 470.0, "y": 1690.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "api_token", "H-API 토큰", False, ""),
            ("message", "timeout_seconds", "요청 제한 시간(초)", False, "30"),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
    "CustomComponent-v5Datalake": {
        "file": "data_analysis_flow/11_datalake_retriever.py",
        "position": {"x": 860.0, "y": 1640.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "module_name", "Datalake 모듈명", False, "lakes"),
            ("message", "class_name", "Datalake 클래스명", False, "LakeHouse"),
            ("message", "user_id", "LakeHouse 사용자 ID", False, ""),
            ("message", "token", "LakeHouse 토큰", False, ""),
            ("message", "s3_access_key", "S3 접근 키", False, ""),
            ("message", "s3_secret_key", "S3 보안 키", False, ""),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
    "CustomComponent-v5Goodocs": {
        "file": "data_analysis_flow/12_goodocs_retriever.py",
        "position": {"x": 860.0, "y": 1840.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "user_id", "Goodocs 사용자 ID", False, ""),
            ("message", "token_source", "Goodocs 토큰 소스", False, ""),
            ("message", "token_key", "Goodocs 토큰 키", False, ""),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
    "CustomComponent-v5RuntimeCleanup": {
        "file": "data_analysis_flow/24_runtime_payload_cleanup.py",
        "position": {"x": 3070.0, "y": 2580.0},
        "inputs": [
            ("data", "payload", "응답 페이로드", True, None),
            ("dropdown", "gc_mode", "GC 모드", False, "generation_0"),
        ],
        "outputs": [("Data", "payload_out", "정리된 페이로드", "build_payload")],
    },
}


def _sticky_note_node(
    note_id: str,
    description: str,
    *,
    x: float,
    y: float,
    width: int,
    height: int,
    color: str,
) -> dict[str, Any]:
    """Build a Langflow 1.9.2 noteNode without execution handles or edges."""

    return {
        "data": {
            "id": note_id,
            "node": {
                "description": description,
                "display_name": "",
                "documentation": "",
                "template": {"backgroundColor": color},
                "lf_version": TARGET_LANGFLOW_VERSION,
            },
            "type": "note",
        },
        "dragging": False,
        "height": height,
        "id": note_id,
        "position": {"x": x, "y": y},
        "resizing": False,
        "selected": False,
        "type": "noteNode",
        "width": width,
        "positionAbsolute": {"x": x, "y": y},
        "style": {"height": height, "width": width},
    }


def _stage_note_specs(variant: str) -> list[dict[str, Any]]:
    is_v2 = variant == "v2"
    analysis_description = (
        "## ⑤ V2 Hybrid 분석\n\n"
        "- **14A 실행 게이트**: 필수 source 조회 성공 여부를 확인합니다.\n"
        "- **14B Fast 경로 판정기**: 단일 source와 완전한 canonical 계약이면 Fast, 그 외에는 Complex로 확정합니다.\n"
        "- **15 Helper 계약 / 16 지연 Prompt**: helper 선택은 공통 수행하고, 전체 pandas Prompt는 Complex에서만 생성합니다.\n"
        "- **17 V2 Hybrid 실행기**: Fast는 고정 함수를 실행하고, Complex만 pandas 생성 모델과 1회 복구를 사용합니다.\n"
        "- **23 결과 저장소**: 결과와 원본 참조를 MongoDB에 저장하고 다운로드 정보를 만듭니다.\n\n"
        "Fast 판정 실패는 오류가 아니라 안전한 Complex 전환입니다."
        if is_v2
        else
        "## ⑤ pandas 분석\n\n"
        "- **14A 실행 게이트**: 필수 source 조회 성공 여부를 확인합니다.\n"
        "- **15 pandas 변수 생성기**: 조회 결과와 실행 계약을 프롬프트 변수로 만듭니다.\n"
        "- **15A Helper 선택기**: 의도 계획에 선언된 Function Case만 코드에 포함합니다.\n"
        "- **pandas Prompt + Language Model**: 계약에 맞는 분석 코드를 생성합니다.\n"
        "- **17 실행/복구기**: 코드를 제한된 환경에서 실행하고 실패 시 최대 1회 복구합니다.\n"
        "- **23 결과 저장소**: 결과와 원본 참조를 MongoDB에 저장합니다."
    )
    answer_description = (
        "## ⑥ 답변 구성\n\n"
        "- **18 지연 Answer Prompt**: Complex에서만 중복 제거된 답변 context와 Prompt를 생성합니다.\n"
        "- **20 V2 Hybrid 답변 생성기**: Fast는 항상 고정 형식으로 답변하며, Complex는 BoolInput 설정에 따라 LLM 또는 고정 답변을 사용합니다.\n"
        "- **답변 도메인 가이드**: 메타데이터 기반 표현 규칙을 Prompt에 제공합니다.\n"
        "- **세션 상태 저장기**: 후속 질문에 필요한 압축 상태를 기록합니다."
        if is_v2
        else
        "## ⑥ 답변 구성\n\n"
        "- **18 답변 변수 생성기**: 질문·조건·결과·진단을 답변 입력으로 정리합니다.\n"
        "- **Answer Prompt + Language Model**: 자연어 답변 초안을 생성합니다.\n"
        "- **20 답변 응답 생성기**: 결과 표·오류·다운로드 정보를 최종 payload에 결합합니다.\n"
        "- **답변 도메인 가이드**: 메타데이터 기반 표현 규칙을 Prompt에 제공합니다.\n"
        "- **세션 상태 저장기**: 후속 질문에 필요한 압축 상태를 기록합니다."
    )
    return [
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}01-entry-state",
            "description": (
                "## ① 요청·세션 입력\n\n"
                "- **Chat Input**: Playground 질문을 받습니다.\n"
                "- **00 세션 상태 로더**: 같은 session의 이전 분석 상태를 읽습니다.\n"
                "- **00 분석 요청 로더**: 질문·이전 상태를 payload로 만듭니다.\n"
                "- **01E 후속 질문 힌트**: 새 분석인지 후속 분석인지 판단할 최소 문맥을 준비합니다."
            ),
            "x": -2400.0,
            "width": 1300,
            "height": 500,
            "color": "blue",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}02-metadata-intent",
            "description": (
                "## ② 메타데이터 기반 의도 분석\n\n"
                "- **01A/01B/01C 로더**: 도메인·테이블 카탈로그·Main Flow Filter를 읽습니다.\n"
                "- **01D 후보 생성기**: 질문과 관련된 항목만 제한된 크기로 고릅니다.\n"
                "- **02 의도 변수 생성기**: 질문·상태·후보·출력 schema를 Prompt 변수로 만듭니다.\n"
                "- **특화 Prompt 입력**: 메타데이터를 해석하는 공통 지침을 제공합니다.\n"
                "- **Intent Prompt + Language Model**: 조회·분석·출력 계약 초안을 만듭니다.\n"
                "- **04 정규화기**: 모델별 표현 차이를 canonical intent_plan으로 정리합니다."
            ),
            "x": -1000.0,
            "width": 1800,
            "height": 560,
            "color": "blue",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}03-contract-history",
            "description": (
                "## ③ 신뢰 계약·후속 결과 복원\n\n"
                "- **04A 카탈로그 Hydrator**: 선택된 dataset의 실제 조회 설정과 컬럼 계약을 보강합니다.\n"
                "- **05 이전 결과 로더**: 후속 분석에 필요한 저장 결과를 복원합니다.\n"
                "- **05A 상위 결과 바인더**: 이전 결과의 entity 값을 다음 조회 파라미터에 연결합니다.\n"
                "- **06 조회 작업 검증기**: source·metric·필수 파라미터 계약을 조회 전에 확인합니다.\n\n"
                "실행 컬럼과 조건은 하드코딩하지 않고 선택된 메타데이터 계약을 따릅니다."
            ),
            "x": 900.0,
            "width": 1700,
            "height": 500,
            "color": "amber",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}04-retrieval",
            "description": (
                "## ④ 데이터 조회·병합\n\n"
                "- **07 라우터**: retrieval job을 source_type별로 분배합니다.\n"
                "- **08 Dummy / 09 Oracle / 10 H-API / 11 Datalake / 12 Goodocs**: 각 공급자에서 원본 데이터를 조회합니다.\n"
                "- **13 결과 병합기**: 공급자별 결과를 source_alias 기준으로 한 payload에 모읍니다.\n"
                "- **14 조회 어댑터**: 공통 runtime_sources/source_results 형식으로 정리합니다.\n\n"
                "분기 노드는 서로 병렬이며, Sticky Note는 실행 연결을 갖지 않습니다."
            ),
            "x": 2700.0,
            "width": 1700,
            "height": 520,
            "color": "blue",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}05-analysis",
            "description": analysis_description,
            "x": 4500.0,
            "width": 2100,
            "height": 570,
            "color": "amber" if is_v2 else "blue",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}06-answer",
            "description": answer_description,
            "x": 6700.0,
            "width": 2100 if not is_v2 else 1700,
            "height": 500,
            "color": "blue",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}07-output",
            "description": (
                "## ⑦ 상태 정리·최종 출력\n\n"
                "- **24 런타임 정리기**: 큰 임시 row buffer를 제거해 최종 payload를 가볍게 만듭니다.\n"
                "- **21 메시지 어댑터**: Playground와 API client에 표시할 Markdown 답변을 만듭니다.\n"
                "- **22 API 응답 생성기**: 웹/API용 구조화 응답을 제공합니다.\n"
                "- **Chat Output**: Playground에 최종 답변을 표시합니다."
            ),
            "x": 9000.0 if not is_v2 else 8500.0,
            "width": 2100,
            "height": 500,
            "color": "amber",
        },
    ]


def _v2_recipe_note_specs() -> list[dict[str, Any]]:
    return [
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}08-v2-route",
            "description": (
                "## V2 Fast / Complex 판정 기준\n\n"
                "**Fast**: 단일 external source, 지원 operation, canonical 컬럼 확정, 고정 결과 schema, bounded 실행 조건을 모두 만족할 때만 선택합니다.\n\n"
                "**Complex**: join·다중 source·사용자 함수·불완전 계약·동적 계산은 기존 LLM pandas 경로로 보냅니다.\n\n"
                "판정 결과는 `analysis_route`, `fast_path_candidate`, `fast_path_recipe`로 payload와 실행 정보에 남습니다."
            ),
            "x": 4800.0,
            "width": 700,
            "height": 520,
            "color": "amber",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}09-v2-fast-basic",
            "description": (
                "## V2 Fast Recipe · 기본 10종\n\n"
                "- **detail_query**: 필터 후 선택 컬럼 상세 목록\n"
                "- **scalar_summary**: 전체 sum·mean·min·max·count 등 단일 요약\n"
                "- **group_summary**: 그룹별 집계\n"
                "- **ranked_summary**: 상위·하위 N 정렬 결과\n"
                "- **frequency_summary**: 값별 빈도·건수\n"
                "- **distinct_summary**: 고유값 목록\n"
                "- **list_summary**: 그룹별 고유 항목 LIST\n"
                "- **existence_summary**: 조건 데이터 존재 여부\n"
                "- **quality_summary**: null·blank·중복 품질 요약\n"
                "- **latest_earliest**: 정렬 기준 최신·최초 행"
            ),
            "x": 5550.0,
            "width": 800,
            "height": 700,
            "color": "blue",
        },
        {
            "id": f"{DATA_ANALYSIS_NOTE_PREFIX}10-v2-fast-advanced",
            "description": (
                "## V2 Fast Recipe · 고급 9종\n\n"
                "- **percent_of_total**: 전체·partition 대비 구성비\n"
                "- **rank_within_group**: 그룹 내부 순위\n"
                "- **threshold_after_aggregate**: 집계 후 임계값 필터\n"
                "- **time_bucket_summary**: 일·주·월·분기·연도 버킷 집계\n"
                "- **period_change**: 전기 대비 증감량·증감률\n"
                "- **running_total**: 시간 순 누적값\n"
                "- **moving_aggregate**: 이동합·이동평균\n"
                "- **percentile_summary**: 연속·이산 백분위수\n"
                "- **pivot_summary**: 제한된 열 수의 pivot/crosstab\n\n"
                "각 recipe는 필요한 calculation 계약이 완전할 때만 Fast로 실행됩니다."
            ),
            "x": 6400.0,
            "width": 850,
            "height": 700,
            "color": "amber",
        },
    ]


def apply_data_analysis_canvas(flow: dict[str, Any], variant: str = "v5") -> None:
    """Apply a non-overlapping flow layout and informational Sticky Notes."""

    if variant not in {"v5", "v2"}:
        raise ValueError(f"unsupported data analysis canvas variant: {variant}")
    nodes = flow.get("data", {}).get("nodes", [])
    nodes[:] = [node for node in nodes if not str(node.get("id") or "").startswith(DATA_ANALYSIS_NOTE_PREFIX)]
    flow["data"]["viewport"] = {"x": 330.0, "y": 250.0, "zoom": 0.12}

    layout = dict(DATA_ANALYSIS_V5_LAYOUT)
    if variant == "v2":
        layout.update(DATA_ANALYSIS_V2_LAYOUT_OVERRIDES)
    for node in nodes:
        position = layout.get(str(node.get("id") or ""))
        if position is None:
            continue
        node["position"] = {"x": position[0], "y": position[1]}
        if isinstance(node.get("positionAbsolute"), dict):
            node["positionAbsolute"] = {"x": position[0], "y": position[1]}

    specs = _stage_note_specs(variant)
    if variant == "v2":
        specs.extend(_v2_recipe_note_specs())
    for index, spec in enumerate(specs):
        nodes.append(
            _sticky_note_node(
                spec["id"],
                spec["description"],
                x=spec["x"],
                y=-1600.0 if index < 7 else 2100.0,
                width=spec["width"],
                height=spec["height"],
                color=spec["color"],
            )
        )


def build_flow(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    raise RuntimeError(
        "The V1 Data Analysis builder is retired. Use tools/build_data_analysis_flow_v2.py "
        "or tools/build_data_analysis_flow_v2_continuation.py."
    )
def _component_path(relative_path: str) -> Path:
    return ROOT / "langflow_components" / relative_path


def _find_native_component(value: Any, display_name: str) -> dict[str, Any]:
    """중첩 component index에서 표시 이름이 일치하는 기본 노드를 찾습니다."""

    if isinstance(value, dict):
        if value.get("display_name") == display_name and isinstance(value.get("template"), dict):
            return deepcopy(value)
        for child in value.values():
            found = _find_native_component(child, display_name)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_native_component(child, display_name)
            if found:
                return found
    return {}


def _rename_node(
    node_index: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    old_node_id: str,
    new_node_id: str,
) -> dict[str, Any]:
    """노드 ID와 연결의 source/target ID를 함께 바꿔 이전 Agent 흔적을 제거합니다."""

    node = node_index.pop(old_node_id)
    node["id"] = new_node_id
    node["data"]["id"] = new_node_id
    node_index[new_node_id] = node
    for edge in edges:
        if edge.get("source") == old_node_id:
            edge["source"] = new_node_id
        if edge.get("target") == old_node_id:
            edge["target"] = new_node_id
    return node


def _apply_native_language_model(
    node: dict[str, Any],
    component_config: dict[str, Any],
    system_message: str,
) -> None:
    """기존 provider 선택값을 보존한 Langflow 기본 Language Model 노드로 교체합니다."""

    previous_template = node["data"]["node"]["template"]
    config = deepcopy(component_config)
    template = config["template"]
    # Langflow 1.10+ index에는 1.9.2에 없던 provider/model_name 입력이 추가됩니다.
    # 생성기를 더 최신 Desktop에서 실행해도 목표 1.9.2 JSON 계약이 달라지지 않게 제거합니다.
    for field_name in ("model_name", "provider"):
        template.pop(field_name, None)
    config["field_order"] = [
        field_name
        for field_name in config.get("field_order", [])
        if field_name not in {"model_name", "provider"}
    ]
    for field_name in ("model", "api_key"):
        previous = previous_template.get(field_name)
        current = template.get(field_name)
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        for attribute in ("value", "load_from_db", "advanced", "show"):
            if attribute in previous:
                current[attribute] = deepcopy(previous[attribute])
    _set_default_language_model(template)
    template["system_message"]["value"] = system_message
    template["stream"]["value"] = False
    template["temperature"]["value"] = 0.1
    template["max_tokens"]["value"] = 8192
    target_code = TARGET_LANGUAGE_MODEL_SOURCE.read_text(encoding="utf-8")
    template["code"]["value"] = target_code
    config.setdefault("metadata", {})["code_hash"] = hashlib.sha256(
        target_code.encode("utf-8")
    ).hexdigest()[:12]
    node["data"]["type"] = "LanguageModelComponent"
    node["data"]["node"] = config


def _replace_edge_source_output(
    edges: list[dict[str, Any]],
    source_id: str,
    old_output: str,
    new_output: str,
) -> None:
    """기본 Agent의 response 포트를 Language Model의 text_output 포트로 바꿉니다."""

    for edge in edges:
        source_handle = edge.get("data", {}).get("sourceHandle", {})
        if edge.get("source") == source_id and source_handle.get("name") == old_output:
            source_handle["name"] = new_output


def _apply_standalone_defaults(nodes: list[dict[str, Any]]) -> None:
    for node in nodes:
        node_type = str(node.get("data", {}).get("type") or "")
        template = node.get("data", {}).get("node", {}).get("template", {})
        if not isinstance(template, dict):
            continue
        # Native LanguageModel nodes and the V2 hybrid executor/answer nodes
        # both expose the same ModelInput.  Apply the default to either shape
        # so the selected model cannot drift when a graph is rebuilt.
        _set_default_language_model(template)
        if node_type == "LanguageModelComponent":
            # Tool이 없는 모델 실행은 기본 Language Model로 처리해 tools 필드를 전송하지 않습니다.
            for field_name, value in (
                ("max_tokens", 8192),
                ("stream", False),
                ("temperature", 0.1),
            ):
                field = template.get(field_name)
                if isinstance(field, dict):
                    field["value"] = value
        for field_name, field in template.items():
            if not isinstance(field, dict):
                continue
            if field_name == "should_store_message":
                # 직접 Playground에서는 Langflow message 저장이 꺼지면 완성된 ChatOutput도 화면에 나타나지 않습니다.
                # Router의 nested 호출만 request tweak로 저장을 끄고, child Flow 기본값은 direct 실행을 위해 켭니다.
                field["value"] = True
            if field_name == "mongo_uri":
                # 실제 URI를 JSON에 넣지 않고 Langflow Credential Global Variable을
                # standalone 노드 입력으로 명시 바인딩합니다. OS 환경변수는 사용하지 않습니다.
                field["value"] = MONGO_GLOBAL_VARIABLE
                field["load_from_db"] = True
                field["advanced"] = False
                field["show"] = True
            if field_name in {"mongo_database", "collection_name", "session_collection_name"}:
                field["load_from_db"] = False
                field["advanced"] = False
                field["show"] = True
            value = field.get("value")
            if isinstance(value, str) and "agent_v5" in value:
                field["value"] = value.replace("agent_v5", "agent_v4")


def _set_default_language_model(template: dict[str, Any]) -> None:
    """Select the supported default model while preserving provider metadata."""

    model = template.get("model")
    if not isinstance(model, dict) or model.get("type") != "model":
        return
    value = model.get("value")
    if isinstance(value, list) and value and isinstance(value[0], dict):
        selected = deepcopy(value[0])
    elif isinstance(value, dict):
        selected = deepcopy(value)
    else:
        selected = {
            "icon": "GoogleGenerativeAI",
            "metadata": {
                "api_key_param": "google_api_key",
                "context_length": 128000,
                "max_tokens_field_name": "max_output_tokens",
                "model_class": "ChatGoogleGenerativeAIFixed",
                "model_name_param": "model",
            },
            "provider": "Google Generative AI",
        }
    selected["name"] = DEFAULT_LANGUAGE_MODEL
    selected.setdefault("provider", "Google Generative AI")
    selected.setdefault("icon", "GoogleGenerativeAI")
    model["value"] = [selected]
    options = model.get("options")
    if isinstance(options, list) and not any(
        isinstance(option, dict) and option.get("name") == DEFAULT_LANGUAGE_MODEL
        for option in options
    ):
        option = deepcopy(selected)
        option.setdefault("category", option.get("provider", "Google Generative AI"))
        options.insert(0, option)


def _refresh_component_node(node: dict[str, Any], path: Path) -> None:
    code = path.read_text(encoding="utf-8")
    class_match = re.search(r"^class\s+(\w+)\([^\n)]*Component\):", code, flags=re.MULTILINE)
    display_match = re.search(r'^\s+display_name\s*=\s*"([^"]+)"', code, flags=re.MULTILINE)
    description_match = re.search(r'^\s+description\s*=\s*"([^"]+)"', code, flags=re.MULTILINE)
    if not class_match or not display_match:
        raise ValueError(f"component metadata parse failed: {path}")
    component = node["data"]["node"]
    component["template"]["code"]["value"] = code
    component["display_name"] = display_match.group(1)
    component["description"] = description_match.group(1) if description_match else ""
    component["lf_version"] = TARGET_LANGFLOW_VERSION
    component.setdefault("metadata", {})["code_hash"] = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
    component["metadata"]["module"] = f"custom_components.{path.stem}"
    node["data"]["type"] = class_match.group(1)
    # Data Analysis builder는 전체 custom component를 다시 import하지 않고 원본 코드를 빠르게 동기화합니다.
    # 따라서 Langflow가 Component.__init__에서 읽는 graph output 선언도 같은 Python 원본에서 일반 규칙으로 반영합니다.
    declared_output = _declared_component_bool(code, "is_output")
    if declared_output is None:
        component.pop("is_output", None)
    else:
        component["is_output"] = declared_output


def _declared_component_bool(code: str, attribute: str) -> bool | None:
    """Component.__init__의 self.<attribute> 불리언 선언을 찾아 frontend node 설정으로 동기화합니다."""

    match = re.search(
        rf"^\s+self\.{re.escape(attribute)}\s*=\s*(True|False)\s*(?:#.*)?$",
        str(code or ""),
        flags=re.MULTILINE,
    )
    if match is None:
        return None
    return match.group(1) == "True"


def _refresh_edge_source_types(edges: list[dict[str, Any]], node_index: dict[str, dict[str, Any]]) -> None:
    """현재 node 계약으로 edge의 data·문자열 handle·ID를 함께 다시 직렬화합니다."""

    # Langflow JSON은 같은 handle을 edge.data, sourceHandle/targetHandle 문자열,
    # edge ID에 중복 보관합니다. 기본 컴포넌트 type이나 output port가 바뀌면 세 위치를
    # 모두 갱신해야 import 시 연결이 제거되지 않습니다.
    for index, edge in enumerate(list(edges)):
        source_id = str(edge.get("source") or "")
        target_id = str(edge.get("target") or "")
        source_data = edge.get("data", {}).get("sourceHandle", {})
        target_data = edge.get("data", {}).get("targetHandle", {})
        source_name = str(source_data.get("name") or "") if isinstance(source_data, dict) else ""
        target_name = str(target_data.get("fieldName") or "") if isinstance(target_data, dict) else ""
        if source_id not in node_index or target_id not in node_index or not source_name or not target_name:
            continue
        edges[index] = _make_edge(node_index, source_id, source_name, target_id, target_name)


def _apply_component_spec(
    node: dict[str, Any],
    inputs: list[tuple[str, str, str, bool, Any]],
    outputs: list[tuple[str, str, str, str]],
    node_index: dict[str, dict[str, Any]],
) -> None:
    component = node["data"]["node"]
    code_template = component["template"]["code"]
    type_template = component["template"]["_type"]
    template: dict[str, Any] = {"_type": type_template, "code": code_template}
    for kind, name, display_name, required, value in inputs:
        template[name] = _input_template(kind, name, display_name, required, value, node_index)
    component["template"] = template
    component["field_order"] = [name for _, name, _, _, _ in inputs]
    component["outputs"] = [
        _output_template(output_type, name, display_name, method, node_index)
        for output_type, name, display_name, method in outputs
    ]
    if len(component["outputs"]) > 1:
        for output in component["outputs"]:
            output["group_outputs"] = True
    component["base_classes"] = list(dict.fromkeys(output_type for output_type, *_ in outputs))


def _input_template(
    kind: str,
    name: str,
    display_name: str,
    required: bool,
    value: Any,
    node_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if kind == "data":
        template = deepcopy(node_index["CustomComponent-5o0CN"]["data"]["node"]["template"]["payload"])
    elif kind == "message":
        template = deepcopy(node_index["CustomComponent-xpbhS"]["data"]["node"]["template"]["question"])
        template.pop("options", None)
        template["tool_mode"] = False
    elif kind == "multiline":
        template = deepcopy(node_index["Agent-nSPco"]["data"]["node"]["template"]["system_prompt"])
    elif kind == "model":
        template = deepcopy(node_index["Agent-nSPco"]["data"]["node"]["template"]["model"])
    elif kind == "secret":
        template = deepcopy(node_index["Agent-nSPco"]["data"]["node"]["template"]["api_key"])
    elif kind == "dropdown":
        template = deepcopy(node_index["CustomComponent-x6NXu"]["data"]["node"]["template"]["retrieval_mode"])
        if name == "max_repair_attempts":
            template["options"] = ["0", "1"]
        elif name == "gc_mode":
            template["options"] = ["disabled", "generation_0", "full"]
        else:
            template["options"] = ["dummy", "live"]
    else:
        raise ValueError(kind)
    template.update({"name": name, "display_name": display_name, "required": required})
    if value is not None or kind not in {"model", "secret"}:
        template["value"] = "" if value is None else value
    template["advanced"] = name in {
        "max_domain_items",
        "min_table_items",
        "max_table_items",
        "max_bytes",
        "max_attempts",
        "max_repair_attempts",
        "max_result_rows",
        "max_source_rows_per_alias",
        "max_document_bytes",
        "gc_mode",
        "api_key",
    }
    return template


def _output_template(
    output_type: str,
    name: str,
    display_name: str,
    method: str,
    node_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if output_type == "Message":
        template = deepcopy(node_index["CustomComponent-fc0Vb"]["data"]["node"]["outputs"][0])
    else:
        template = deepcopy(node_index["CustomComponent-5o0CN"]["data"]["node"]["outputs"][0])
    template.update(
        {
            "name": name,
            "display_name": display_name,
            "method": method,
            "selected": output_type,
            "types": [output_type],
            "group_outputs": False,
        }
    )
    return template


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        edge["source"],
        edge["data"]["sourceHandle"]["name"],
        edge["target"],
        edge["data"]["targetHandle"]["fieldName"],
    )


def _make_edge(
    node_index: dict[str, dict[str, Any]],
    source_id: str,
    source_name: str,
    target_id: str,
    target_name: str,
) -> dict[str, Any]:
    source_node = node_index[source_id]
    target_node = node_index[target_id]
    source_output = next(item for item in source_node["data"]["node"]["outputs"] if item["name"] == source_name)
    target_input = target_node["data"]["node"]["template"][target_name]
    output_types = source_output.get("types") or [source_output.get("selected") or "Data"]
    input_types = target_input.get("input_types") or (["Message"] if target_input.get("type") == "str" else ["Data"])
    source_handle = {
        "dataType": source_node["data"]["type"],
        "id": source_id,
        "name": source_name,
        "output_types": output_types,
    }
    target_handle = {
        "fieldName": target_name,
        "id": target_id,
        "inputTypes": input_types,
        "type": target_input.get("type") or "other",
    }
    source_text = _source_handle_text(source_handle)
    target_text = _target_handle_text(target_handle)
    return {
        "animated": False,
        "className": "",
        "data": {"sourceHandle": source_handle, "targetHandle": target_handle},
        "id": f"xy-edge__{source_id}{source_text}-{target_id}{target_text}",
        "selected": False,
        "source": source_id,
        "sourceHandle": source_text,
        "target": target_id,
        "targetHandle": target_text,
    }


def _source_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _target_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _handle_text(value: dict[str, Any]) -> str:
    """Mirror Langflow frontend's stable stringify + quote substitution."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace('"', "œ")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retired V1 builder helper. Use build_data_analysis_flow_v2.py instead."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    flow = build_flow(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"output": str(args.output), "nodes": len(flow["data"]["nodes"]), "edges": len(flow["data"]["edges"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
