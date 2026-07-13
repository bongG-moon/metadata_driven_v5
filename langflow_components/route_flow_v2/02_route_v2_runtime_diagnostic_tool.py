# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 Route V2 실행 환경 진단 도구
# 역할: 현재 Router 실행 사용자 기준의 하위 Flow 가시성, 이름 중복, Langflow/LFX 호환성과 하위 그래프 구조를 안전하게 점검합니다.
# 주요 입력: 대상 Flow 이름 목록 (target_flow_names_json), 기대 Langflow 버전, 기대 LFX 버전
# 주요 출력: 실행 환경 진단 도구 (component_as_tool)
# 처리 흐름: 사용자가 명시적으로 진단을 요청했을 때만 현재 사용자에게 보이는 Flow 목록을 읽고 사전 점검 결과를 반환합니다.
# 유지보수 포인트: 하위 Flow를 실제 실행하지 않으며 사용자/Flow/session 원본 ID, API Key, 환경변수와 예외 원문을 출력하지 않습니다.
# =============================================================================

from __future__ import annotations

import hashlib
import inspect
import json
import re
import socket
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from uuid import UUID

from langchain_core.tools import StructuredTool
from lfx.custom.custom_component.component import Component
from lfx.io import MultilineInput, Output, StrInput
from lfx.schema.message import Message
from pydantic import BaseModel, Field


DEFAULT_TARGET_FLOW_NAMES = [
    "metadata_driven_v5_data_analysis_standalone",
    "metadata_driven_v5_metadata_qa_standalone",
    "metadata_driven_v5_domain_saving_standalone",
    "metadata_driven_v5_table_catalog_saving_standalone",
    "metadata_driven_v5_main_flow_filter_saving_standalone",
]
EXPECTED_LANGFLOW_VERSION = "1.8.2"
EXPECTED_LFX_VERSION = "0.3.4"
DIAGNOSTIC_TOOL_NAME = "diagnose_route_v2_environment"
REQUIRED_RUN_FLOW_METHODS = {
    "get_flow": {"flow_name_selected", "flow_id_selected"},
    "get_graph": {"flow_name_selected", "flow_id_selected", "updated_at"},
    "_sync_flow_outputs": {"outputs"},
    "_get_cached_run_outputs": {"user_id", "output_type"},
    "_resolve_flow_output": {"vertex_id", "output_name"},
    "_get_tools": set(),
    "_pre_run_setup": set(),
}


# 내부 연동 도우미 클래스: `RouteV2DiagnosticRequest`는 Agent가 진단 의도를 명시적으로 전달할 수 있는 작은 고정 schema입니다.
class RouteV2DiagnosticRequest(BaseModel):
    diagnostic_request: str = Field(
        default="Route V2의 Run Flow 사용자·이름·버전 호환성을 진단해줘",
        description="진단이 필요한 Route V2 또는 Run Flow 오류 상황을 짧게 적습니다.",
    )


# 함수 설명: `_fingerprint()`는 내부 식별자를 원문 복원이 어려운 짧은 SHA-256 참조값으로 바꿉니다.
def _fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else ""


# 함수 설명: `_valid_uuid()`는 Langflow 실행 사용자 ID가 내부 조회에서 사용할 수 있는 UUID 형식인지 원문 노출 없이 판정합니다.
def _valid_uuid(value: Any) -> bool:
    try:
        UUID(str(value or "").strip())
        return True
    except Exception:
        return False


# 함수 설명: `_package_version()`은 설치된 package 버전만 읽고 미설치 또는 조회 실패를 안전한 상태값으로 바꿉니다.
def _package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "not_installed"
    except Exception:
        return "unknown"


# 함수 설명: `_target_names()`는 JSON 배열 또는 줄바꿈 입력을 중복 없는 Flow 이름 목록으로 정규화합니다.
def _target_names(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        text = str(value or "").strip()
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        raw_items = parsed if isinstance(parsed, list) else re.split(r"[\r\n,]+", text)
    result: list[str] = []
    for item in raw_items:
        name = str(item or "").strip()
        if name and name not in result:
            result.append(name)
    return result or list(DEFAULT_TARGET_FLOW_NAMES)


# 함수 설명: `_flow_payload()`는 Langflow Data 또는 dict에서 Flow 메타데이터 dict만 안전하게 꺼냅니다.
def _flow_payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return data if isinstance(data, dict) else {}


# 함수 설명: `_suffix_variant()`는 동일 기본 이름 뒤에 Langflow 중복 import 번호가 붙은 경우 suffix만 반환합니다.
def _suffix_variant(expected_name: str, actual_name: str) -> str:
    match = re.fullmatch(re.escape(expected_name) + r"\s*(\(\d+\))", actual_name)
    return match.group(1) if match else ""


# 함수 설명: `_node_type()`은 Flow JSON 노드의 type 또는 표시명을 표준 문자열로 반환합니다.
def _node_type(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    config = data.get("node") if isinstance(data.get("node"), dict) else {}
    return str(data.get("type") or config.get("display_name") or "")


# 함수 설명: `_graph_contract()`는 하위 Flow를 실행하지 않고 직렬화된 노드·edge만으로 단일 Chat I/O 계약을 점검합니다.
def _graph_contract(flow_payload: dict[str, Any]) -> dict[str, Any]:
    graph_data = flow_payload.get("data") if isinstance(flow_payload.get("data"), dict) else {}
    nodes = graph_data.get("nodes") if isinstance(graph_data.get("nodes"), list) else []
    edges = graph_data.get("edges") if isinstance(graph_data.get("edges"), list) else []
    chat_inputs = [node for node in nodes if _node_type(node) in {"ChatInput", "Chat Input"}]
    chat_outputs = [node for node in nodes if _node_type(node) in {"ChatOutput", "Chat Output"}]
    outgoing = {
        str(edge.get("source") or "")
        for edge in edges
        if isinstance(edge, dict) and str(edge.get("source") or "")
    }
    terminal_output_count = 0
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("id") or "") in outgoing:
            continue
        config = node.get("data", {}).get("node", {}) if isinstance(node.get("data"), dict) else {}
        outputs = config.get("outputs") if isinstance(config, dict) else []
        if isinstance(outputs, list):
            terminal_output_count += len([item for item in outputs if isinstance(item, dict) and item.get("name")])
    contract_ok = len(chat_inputs) == 1 and len(chat_outputs) == 1 and terminal_output_count == 1
    return {
        "node_count": len(nodes),
        "chat_input_count": len(chat_inputs),
        "chat_output_count": len(chat_outputs),
        "terminal_output_count": terminal_output_count,
        "contract_ok": contract_ok,
    }


# 함수 설명: `_run_flow_contract()`는 Route V2가 의존하는 RunFlowBase 내부 method와 필수 parameter가 현재 server에 존재하는지 검사합니다.
def _run_flow_contract() -> dict[str, Any]:
    loaded_class = None
    loaded_module = ""
    for module_name in ("lfx.base.tools.run_flow", "langflow.base.tools.run_flow"):
        try:
            module = import_module(module_name)
            loaded_class = getattr(module, "RunFlowBaseComponent", None)
        except Exception:
            loaded_class = None
        if loaded_class is not None:
            loaded_module = module_name
            break
    if loaded_class is None:
        return {
            "module": "unavailable",
            "contract_ok": False,
            "missing_methods": sorted(REQUIRED_RUN_FLOW_METHODS),
            "signature_mismatches": [],
        }

    missing_methods: list[str] = []
    signature_mismatches: list[str] = []
    for method_name, required_params in REQUIRED_RUN_FLOW_METHODS.items():
        method = getattr(loaded_class, method_name, None)
        if not callable(method):
            missing_methods.append(method_name)
            continue
        if not required_params:
            continue
        try:
            actual_params = set(inspect.signature(method).parameters) - {"self"}
        except Exception:
            signature_mismatches.append(method_name)
            continue
        if not required_params.issubset(actual_params):
            signature_mismatches.append(method_name)
    return {
        "module": loaded_module,
        "contract_ok": not missing_methods and not signature_mismatches,
        "missing_methods": sorted(missing_methods),
        "signature_mismatches": sorted(signature_mismatches),
    }


# 함수 설명: `_visible_flow_index()`는 현재 사용자에게 보이는 Flow 중 예상 이름과 중복 suffix 후보만 선별합니다.
def _visible_flow_index(flows: Any, target_names: list[str]) -> dict[str, Any]:
    exact: dict[str, dict[str, Any]] = {}
    suffixes: dict[str, list[str]] = {name: [] for name in target_names}
    for item in flows if isinstance(flows, list) else []:
        payload = _flow_payload(item)
        actual_name = str(payload.get("name") or "").strip()
        if actual_name in target_names:
            exact[actual_name] = payload
            continue
        for expected_name in target_names:
            suffix = _suffix_variant(expected_name, actual_name)
            if suffix and suffix not in suffixes[expected_name]:
                suffixes[expected_name].append(suffix)
    return {"exact": exact, "suffixes": suffixes}


# 함수 설명: `_conclusion()`은 사용자·가시성·그래프·버전 검사 결과를 우선순위에 따라 한 개의 진단 코드로 결정합니다.
def _conclusion(report: dict[str, Any]) -> tuple[str, str, list[str]]:
    identity = report.get("identity", {})
    runtime = report.get("runtime", {})
    targets = report.get("targets", [])
    if not identity.get("runtime_user_present"):
        return (
            "RUNTIME_USER_MISSING",
            "Route V2 실행 문맥에 사용자 ID가 없어 내부 Run Flow가 하위 Flow를 조회할 수 없습니다.",
            ["Kubernetes API 인증과 Router 실행 user_id 전달 설정을 확인하세요."],
        )
    if not identity.get("runtime_user_uuid_valid"):
        return (
            "RUNTIME_USER_INVALID",
            "Route V2 실행 사용자 ID가 Langflow Flow 조회에 사용할 수 있는 UUID 형식이 아닙니다.",
            ["Kubernetes API 인증과 Router component user_id 주입 상태를 확인하세요."],
        )
    if identity.get("component_graph_user_match") is False:
        return (
            "USER_CONTEXT_MISMATCH",
            "컴포넌트 사용자와 부모 graph 사용자 문맥이 다릅니다.",
            ["Router API Key 사용자와 graph 실행 사용자 전달 경로를 확인하세요."],
        )
    if report.get("flow_list_status") != "ok":
        return (
            "CURRENT_USER_FLOW_LIST_FAILED",
            "현재 실행 사용자 범위의 Flow 목록을 읽지 못했습니다.",
            ["Kubernetes backend log에서 Flow 목록 조회 예외를 확인하세요."],
        )
    missing_targets = [item for item in targets if not item.get("exact_visible")]
    if missing_targets:
        with_suffix = [item for item in missing_targets if item.get("suffix_variants")]
        code = "TARGET_NAME_SUFFIX_MISMATCH" if with_suffix else "TARGET_NOT_VISIBLE_IN_RUNTIME_SCOPE"
        message = (
            "정확한 대상 이름 대신 중복 import suffix가 붙은 Flow가 현재 사용자에게 보입니다."
            if with_suffix
            else "현재 실행 사용자 범위에서 정확한 이름의 하위 Flow를 찾지 못했습니다. 이름 또는 소유권 차이일 수 있습니다."
        )
        return (
            code,
            message,
            ["01~05 하위 Flow와 07 Router를 같은 계정으로 가져오고 정확한 Flow 이름을 확인하세요."],
        )
    invalid_graphs = [item for item in targets if not item.get("graph_contract", {}).get("contract_ok")]
    if invalid_graphs:
        return (
            "CHILD_FLOW_TOPOLOGY_MISMATCH",
            "하위 Flow의 Chat Input, Chat Output 또는 terminal output 개수가 Route V2 계약과 다릅니다.",
            ["현재 import-ready 하위 Flow JSON으로 교체하고 Chat Input/Output을 각각 하나로 유지하세요."],
        )
    if not runtime.get("run_flow_contract", {}).get("contract_ok"):
        return (
            "RUN_FLOW_CONTRACT_MISMATCH",
            "현재 Langflow/LFX의 Run Flow 내부 API가 Route V2 구현 계약과 다릅니다.",
            ["Kubernetes backend를 검증된 Langflow 1.8.2 / LFX 0.3.4 조합과 대조하세요."],
        )
    if not runtime.get("version_match"):
        return (
            "RUNTIME_VERSION_MISMATCH",
            "하위 Flow 가시성과 구조는 정상이지만 Kubernetes Langflow/LFX 버전이 검증 기준과 다릅니다.",
            ["모든 backend Pod의 Langflow/LFX 버전을 동일하게 고정하세요."],
        )
    return (
        "PREFLIGHT_OK_CHILD_EXECUTION_FAILED",
        "사용자·이름·그래프·버전 사전 점검은 통과했습니다. 오류는 질문 tweak 또는 하위 Flow 내부 실행 단계에 있습니다.",
        ["동일 시각의 backend log에서 Error running flow 직전 원본 예외를 확인하세요."],
    )


# 주요 함수: 현재 Route V2 실행 문맥에서 하위 Flow 사전 점검을 수행하고 비밀값 없는 진단 report를 만듭니다.
async def diagnose_route_v2_environment(
    *,
    component: Any,
    target_flow_names: Any,
    expected_langflow_version: Any = EXPECTED_LANGFLOW_VERSION,
    expected_lfx_version: Any = EXPECTED_LFX_VERSION,
) -> dict[str, Any]:
    user_id = str(getattr(component, "user_id", "") or "").strip()
    graph = getattr(component, "graph", None)
    graph_user_id = str(getattr(graph, "user_id", "") or "").strip()
    runtime_user_uuid_valid = _valid_uuid(user_id)
    target_names = _target_names(target_flow_names)
    langflow_version = _package_version("langflow")
    lfx_version = _package_version("lfx")
    runtime = {
        "langflow_version": langflow_version,
        "lfx_version": lfx_version,
        "expected_langflow_version": str(expected_langflow_version or EXPECTED_LANGFLOW_VERSION),
        "expected_lfx_version": str(expected_lfx_version or EXPECTED_LFX_VERSION),
        "version_match": (
            langflow_version == str(expected_langflow_version or EXPECTED_LANGFLOW_VERSION)
            and lfx_version == str(expected_lfx_version or EXPECTED_LFX_VERSION)
        ),
        "run_flow_contract": _run_flow_contract(),
        "runtime_instance_ref": _fingerprint(socket.gethostname()),
    }
    identity = {
        "runtime_user_present": bool(user_id),
        "runtime_user_uuid_valid": runtime_user_uuid_valid,
        "runtime_user_ref": _fingerprint(user_id),
        "graph_user_present": bool(graph_user_id),
        "graph_user_ref": _fingerprint(graph_user_id),
        "component_graph_user_match": user_id == graph_user_id if user_id and graph_user_id else None,
    }

    flows: list[Any] = []
    flow_list_status = "not_attempted"
    if runtime_user_uuid_valid:
        try:
            flows = await component.alist_flows()
            flow_list_status = "ok"
        except Exception:
            flow_list_status = "error"
    flow_index = _visible_flow_index(flows, target_names)
    targets = []
    for name in target_names:
        payload = flow_index["exact"].get(name)
        target = {
            "target_name": name,
            "exact_visible": isinstance(payload, dict),
            "suffix_variants": sorted(flow_index["suffixes"].get(name, [])),
        }
        if isinstance(payload, dict):
            target["flow_ref"] = _fingerprint(payload.get("id"))
            target["graph_contract"] = _graph_contract(payload)
        targets.append(target)

    report: dict[str, Any] = {
        "schema_version": "route_v2_runtime_diagnostic_v1",
        "runtime": runtime,
        "identity": identity,
        "flow_list_status": flow_list_status,
        "targets": targets,
    }
    code, message, recommendations = _conclusion(report)
    report["overall_status"] = "ok" if code == "PREFLIGHT_OK_CHILD_EXECUTION_FAILED" else "warning"
    report["conclusion_code"] = code
    report["conclusion"] = message
    report["recommendations"] = recommendations
    return report


# 함수 설명: `_markdown_report()`는 구조화 진단 결과를 사용자가 바로 해석할 수 있는 안전한 Markdown 표로 렌더링합니다.
def _markdown_report(report: dict[str, Any]) -> str:
    runtime = report.get("runtime", {})
    identity = report.get("identity", {})
    lines = [
        "### Route V2 실행 환경 진단",
        "",
        f"- 판정 코드: `{report.get('conclusion_code', 'UNKNOWN')}`",
        f"- 결론: {report.get('conclusion', '')}",
        f"- 실행 사용자 참조: `{identity.get('runtime_user_ref') or '없음'}`",
        f"- Component/Graph 사용자 일치: `{identity.get('component_graph_user_match')}`",
        f"- Langflow/LFX: `{runtime.get('langflow_version')}` / `{runtime.get('lfx_version')}`",
        f"- 검증 기준 버전 일치: `{runtime.get('version_match')}`",
        f"- Run Flow 내부 계약 일치: `{runtime.get('run_flow_contract', {}).get('contract_ok')}`",
        "",
        "| 대상 Flow | 현재 사용자에게 정확한 이름이 보임 | 중복 suffix 후보 | Chat I/O/terminal |",
        "| --- | --- | --- | --- |",
    ]
    for item in report.get("targets", []):
        contract = item.get("graph_contract", {})
        topology = (
            f"{contract.get('chat_input_count')}/{contract.get('chat_output_count')}/{contract.get('terminal_output_count')}"
            if contract
            else "-"
        )
        suffixes = ", ".join(item.get("suffix_variants", [])) or "-"
        lines.append(
            f"| `{item.get('target_name', '')}` | `{item.get('exact_visible')}` | {suffixes} | `{topology}` |"
        )
    recommendations = report.get("recommendations", [])
    if recommendations:
        lines.extend(["", "### 권장 조치"])
        lines.extend(f"- {item}" for item in recommendations)
    lines.extend(
        [
            "",
            "> 이 보고서는 현재 07 Router 요청의 실행 사용자 범위만 검사합니다. 원본 사용자 ID, Flow ID, API Key와 환경변수 값은 표시하지 않습니다.",
        ]
    )
    return "\n".join(lines)


# Langflow 컴포넌트 클래스: 입력한 대상 이름과 검증 기준을 Agent 진단 Tool 실행에 사용합니다.
class RouteV2RuntimeDiagnosticTool(Component):
    display_name = "02 Route V2 실행 환경 진단 도구"
    description = "사용자가 명시적으로 Route V2 또는 Run Flow 오류 진단을 요청할 때만 사용자·이름·버전·그래프 계약을 점검합니다."
    name = "RouteV2RuntimeDiagnosticTool"
    icon = "Stethoscope"

    inputs = [
        MultilineInput(
            name="target_flow_names_json",
            display_name="대상 Flow 이름 목록",
            value=json.dumps(DEFAULT_TARGET_FLOW_NAMES, ensure_ascii=False, indent=2),
            required=True,
            advanced=True,
        ),
        StrInput(
            name="expected_langflow_version",
            display_name="기대 Langflow 버전",
            value=EXPECTED_LANGFLOW_VERSION,
            required=True,
            advanced=True,
        ),
        StrInput(
            name="expected_lfx_version",
            display_name="기대 LFX 버전",
            value=EXPECTED_LFX_VERSION,
            required=True,
            advanced=True,
        ),
    ]
    outputs = [
        Output(
            name="component_as_tool",
            display_name="실행 환경 진단 도구",
            method="build_tool",
            types=["Tool"],
            tool_mode=True,
        )
    ]

    # Langflow 출력 함수: Agent에 연결할 return_direct 진단 Tool 하나를 생성합니다.
    async def build_tool(self) -> list[StructuredTool]:
        tool = StructuredTool.from_function(
            name=DIAGNOSTIC_TOOL_NAME,
            description=(
                "Route V2, Run Flow, user_id, Flow 소유권/정확한 이름, Langflow/LFX 버전 오류를 진단합니다. "
                "일반 데이터 조회·메타데이터 QA·저장 요청에는 절대 사용하지 않습니다."
            ),
            coroutine=self.run_diagnostic,
            args_schema=RouteV2DiagnosticRequest,
            return_direct=True,
        )
        tool.tags = [DIAGNOSTIC_TOOL_NAME]
        return [tool]

    # 함수 설명: `run_diagnostic()`은 현재 Router 요청 문맥으로 사전 점검을 수행하고 단일 Markdown Message를 반환합니다.
    async def run_diagnostic(self, diagnostic_request: str = "") -> Message:
        del diagnostic_request
        report = await diagnose_route_v2_environment(
            component=self,
            target_flow_names=getattr(self, "target_flow_names_json", ""),
            expected_langflow_version=getattr(self, "expected_langflow_version", EXPECTED_LANGFLOW_VERSION),
            expected_lfx_version=getattr(self, "expected_lfx_version", EXPECTED_LFX_VERSION),
        )
        self.status = {
            "overall_status": report.get("overall_status"),
            "conclusion_code": report.get("conclusion_code"),
        }
        return Message(text=_markdown_report(report))
