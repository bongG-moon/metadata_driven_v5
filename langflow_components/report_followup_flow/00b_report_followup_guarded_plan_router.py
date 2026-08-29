# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00B Report 후속 Guarded Plan Router
# 역할: Prompt Builder가 ready로 판정한 요청에만 계획 LLM을 한 번 호출합니다.
# 주요 입력: Report 후속 요청 payload, 제한형 계획 Prompt, Langflow 1.11 언어 모델 설정
# 주요 출력: Plan Normalizer가 소비하는 JSON 텍스트 Message
# 처리 흐름: upstream 상태 확인 -> 비 ready 결정론 응답 또는 ready 모델 1회 호출 -> Message 반환
# 유지보수 포인트: 차단·전환·확인 필요 요청에서는 모델과 provider를 절대 초기화하거나 호출하지 않습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable

from lfx.custom.custom_component.component import Component
from lfx.io import (
    BoolInput,
    DataInput,
    IntInput,
    MessageTextInput,
    ModelInput,
    MultilineInput,
    Output,
    SecretStrInput,
    SliderInput,
    StrInput,
)
from lfx.schema.message import Message


DEFAULT_SYSTEM_MESSAGE = (
    "Plan only from the supplied Report query-source contract. Return exactly one JSON object "
    "and never request, infer, or join a live source."
)
LLM_TEMPERATURE = 0.0
NON_READY_STATUSES = {"blocked", "handoff_required", "clarification_required"}


# 함수 설명: Langflow Data 또는 dict에서 안전한 payload 사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return deepcopy(raw) if isinstance(raw, dict) else {}


# 함수 설명: Message와 LangChain 응답을 Plan Normalizer가 읽을 수 있는 텍스트로 변환합니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", None)
    if text not in (None, ""):
        return str(text).strip()
    content = getattr(value, "content", value)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    if isinstance(content, list):
        blocks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                blocks.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                blocks.append(item)
        return "\n".join(block for block in blocks if block).strip()
    return str(content or "").strip()


# 함수 설명: 비 ready 요청에서 사용자 입력과 무관하게 동일 구조의 제한형 계획 응답을 만듭니다.
def _deterministic_non_ready_plan(payload: dict[str, Any], status: str) -> dict[str, Any]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    errors = trace.get("errors") if isinstance(trace.get("errors"), list) else []
    issue = next((item for item in reversed(errors) if isinstance(item, dict)), {})
    reason = str(issue.get("message") or issue.get("type") or f"report_followup_{status}").strip()
    return {
        "status": status,
        "source_alias": "",
        "operations": [],
        "reason": reason[:500],
    }


# 주요 함수: upstream 상태가 ready일 때만 모델을 한 번 호출하고 그 외에는 결정론 응답을 반환합니다.
def route_report_followup_plan_response(
    payload_value: Any,
    prompt_value: Any = "",
    model_invoker: Callable[[str], Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    payload = _payload(payload_value)
    followup = payload.get("report_followup") if isinstance(payload.get("report_followup"), dict) else {}
    status = str(followup.get("status") or "blocked").strip().casefold()
    if status != "ready":
        normalized_status = status if status in NON_READY_STATUSES else "blocked"
        response = _deterministic_non_ready_plan(payload, normalized_status)
        return json.dumps(response, ensure_ascii=False, separators=(",", ":")), {
            "stage": "00B_report_followup_guarded_plan_router",
            "mode": normalized_status,
            "model_called": False,
            "plan_llm_skipped": True,
            "upstream_status": status,
        }

    prompt = _text(prompt_value)
    if not prompt:
        raise ValueError("Report 후속 계획 Prompt가 비어 있습니다.")
    if model_invoker is None:
        raise RuntimeError("Report 후속 계획 Language Model이 연결되지 않았습니다.")
    response = model_invoker(prompt)
    return _text(response), {
        "stage": "00B_report_followup_guarded_plan_router",
        "mode": "ready",
        "model_called": True,
        "plan_llm_skipped": False,
        "prompt_chars": len(prompt),
    }


# Langflow 컴포넌트 클래스: Report 후속 upstream gate와 실제 계획 모델 호출을 한 경계에서 처리합니다.
class ReportFollowupGuardedPlanRouter(Component):
    display_name = "00B Report 후속 Guarded Plan Router"
    description = "Report 후속 요청이 ready인 경우에만 계획 LLM을 한 번 호출하고, 나머지 상태는 모델 호출 없이 전달합니다."
    name = "ReportFollowupGuardedPlanRouter"
    icon = "ShieldCheck"
    inputs = [
        DataInput(name="payload", display_name="Report 후속 요청 페이로드", required=True),
        MessageTextInput(name="prompt", display_name="Report 후속 계획 Prompt", required=False),
        ModelInput(
            name="model",
            display_name="Language Model",
            info="Select your model provider",
            real_time_refresh=True,
            required=True,
        ),
        StrInput(
            name="model_name",
            display_name="Model Name Override",
            info="Optional model name to use instead of the selected model.",
            advanced=True,
            load_from_db=False,
        ),
        StrInput(
            name="provider",
            display_name="Provider Override",
            info="Optional provider to use with Model Name Override.",
            advanced=True,
            load_from_db=False,
        ),
        SecretStrInput(
            name="api_key",
            display_name="API Key",
            info="Overrides global provider settings. Leave blank to use your pre-configured API Key.",
            required=False,
            show=True,
            real_time_refresh=True,
            advanced=True,
        ),
        MultilineInput(
            name="system_message",
            display_name="System Message",
            value=DEFAULT_SYSTEM_MESSAGE,
            advanced=False,
        ),
        BoolInput(name="stream", display_name="Stream", value=False, advanced=True),
        SliderInput(name="temperature", display_name="Temperature", value=LLM_TEMPERATURE, advanced=True),
        IntInput(name="max_tokens", display_name="Max Tokens", value=1800, advanced=True),
    ]
    outputs = [Output(name="text_output", display_name="Report 후속 계획 응답", method="build_response", types=["Message"])]

    # Langflow 출력 함수: gate 결과에 따라 결정론 JSON 또는 단일 모델 응답 Message를 반환합니다.
    def build_response(self) -> Message:
        text, trace = route_report_followup_plan_response(
            getattr(self, "payload", None),
            getattr(self, "prompt", ""),
            self._invoke_model,
        )
        self.status = trace
        return Message(text=text)

    # 함수 설명: Langflow 1.11 model override 계약을 적용한 뒤 표준 메시지 배열로 모델을 한 번 호출합니다.
    def _invoke_model(self, prompt: str) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage
        from lfx.base.models.unified_models import get_language_model_options, get_llm
        from lfx.components.models_and_agents.model_selection import apply_model_overrides

        model = apply_model_overrides(
            getattr(self, "model", None),
            model_name=getattr(self, "model_name", None),
            provider=getattr(self, "provider", None),
            user_id=getattr(self, "user_id", None),
            get_options=get_language_model_options,
        )
        llm = get_llm(
            model=model,
            user_id=getattr(self, "user_id", None),
            api_key=getattr(self, "api_key", None),
            temperature=getattr(self, "temperature", LLM_TEMPERATURE),
            stream=getattr(self, "stream", False),
            max_tokens=getattr(self, "max_tokens", 1800),
        )
        if llm is None or not hasattr(llm, "invoke"):
            raise RuntimeError("Report 후속 계획 Language Model이 연결되지 않았습니다.")
        messages = []
        system_message = _text(getattr(self, "system_message", ""))
        if system_message:
            messages.append(SystemMessage(content=system_message))
        messages.append(HumanMessage(content=prompt))
        return llm.invoke(messages)

    # 함수 설명: 모델 선택 변경 시 Langflow 1.11 provider별 설정 필드를 동적으로 갱신합니다.
    def update_build_config(self, build_config: dict, field_value: str, field_name: str | None = None):
        from lfx.base.models.unified_models import handle_model_input_update

        return handle_model_input_update(self, build_config, field_value, field_name)
