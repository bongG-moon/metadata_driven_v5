#!/usr/bin/env python3
"""Generate V2 standalone prompt components from the audited common logic.

The generated files intentionally contain their runtime dependencies. Langflow
therefore imports each custom component without relying on repository modules,
while this builder keeps the shared variable/normalization logic single-sourced.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON_ROOT = ROOT / "langflow_components" / "data_analysis_flow"
V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"

PANDAS_CLASS_MARKER = "class PandasVariablesBuilder(Component):"
ANSWER_CLASS_MARKER = "class AnswerVariablesBuilder(Component):"


SELECTOR_CLASS = r'''
# Langflow 컴포넌트 클래스: V2 helper 선택 계약을 독립 실행 가능한 노드로 노출합니다.
class FunctionCaseSelectionBuilder(Component):
    display_name = "15 V2 Function Case 선택 정보 생성기"
    description = "Fast/Complex 공통 helper 선택에 필요한 작은 계약만 생성합니다."
    inputs = [DataInput(name="payload", display_name="경로 결정 페이로드", required=True)]
    outputs = [
        Output(
            name="function_case_selection_json",
            display_name="Function Case 선택 정보 JSON",
            method="build_selection",
            types=["Message"],
        )
    ]

    # 함수 설명: 전체 pandas context 없이 Function Case 선택 정보만 반환합니다.
    def build_selection(self) -> Message:
        """Return only helper-selection metadata, never the full pandas prompt context."""

        return Message(text=build_function_case_selection_only(getattr(self, "payload", None)))
'''


PANDAS_PROMPT_CLASS = r'''
# Langflow 컴포넌트 클래스: V2 경로 결정 뒤 pandas 프롬프트를 지연 생성합니다.
class RouteAwarePandasPromptBuilder(Component):
    display_name = "16 V2 경로 인식 pandas Prompt 생성기"
    description = "Complex 경로에서만 pandas prompt 변수를 직렬화하고 Fast/Blocked에서는 빈 prompt를 반환합니다."
    inputs = [
        DataInput(name="payload", display_name="경로 결정 페이로드", required=True),
        MultilineInput(name="prompt_template", display_name="pandas 프롬프트 템플릿", required=True),
        MessageTextInput(
            name="function_case_helper_code",
            display_name="선택 Function Case Helper",
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(name="pandas_prompt", display_name="경로 인식 pandas Prompt", method="build_prompt", types=["Message"])
    ]

    # 함수 설명: Complex 경로에서만 전체 pandas 생성 프롬프트를 실제 문자열로 만듭니다.
    def build_prompt(self) -> Message:
        """Materialize the full prompt only after the resolver selected Complex."""

        return Message(
            text=build_route_aware_pandas_prompt(
                getattr(self, "payload", None),
                getattr(self, "prompt_template", ""),
                getattr(self, "function_case_helper_code", ""),
            )
        )
'''


ANSWER_PROMPT_CLASS = r'''
# Langflow 컴포넌트 클래스: V2 Complex 답변용 context와 프롬프트를 지연 생성합니다.
class RouteAwareAnswerPromptBuilder(Component):
    display_name = "18 V2 경로 인식 Answer Prompt 생성기"
    description = "Complex 경로에서만 중복을 제거한 답변 context와 prompt를 생성합니다."
    inputs = [
        DataInput(name="payload", display_name="분석 결과 페이로드", required=True),
        MultilineInput(name="prompt_template", display_name="답변 프롬프트 템플릿", required=True),
        MessageTextInput(
            name="domain_answer_guidance",
            display_name="도메인 특화 응답 지침",
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(name="answer_prompt", display_name="경로 인식 Answer Prompt", method="build_prompt", types=["Message"])
    ]

    # 함수 설명: Fast와 Blocked를 건너뛰고 Complex 답변 프롬프트만 반환합니다.
    def build_prompt(self) -> Message:
        """Skip answer context serialization for Fast and Blocked routes."""

        return Message(
            text=build_route_aware_answer_prompt(
                getattr(self, "payload", None),
                getattr(self, "prompt_template", ""),
                getattr(self, "domain_answer_guidance", ""),
            )
        )
'''


def _prefix(path: Path, marker: str) -> str:
    source = path.read_text(encoding="utf-8")
    if marker not in source:
        raise ValueError(f"component class marker not found: {marker} in {path}")
    return source.split(marker, 1)[0].rstrip() + "\n\n"


def render_sources() -> dict[Path, str]:
    pandas_prefix = _prefix(COMMON_ROOT / "15_pandas_variables_builder.py", PANDAS_CLASS_MARKER)
    answer_prefix = _prefix(COMMON_ROOT / "18_answer_variables_builder.py", ANSWER_CLASS_MARKER)
    return {
        V2_ROOT / "15_function_case_selection_builder.py": pandas_prefix + SELECTOR_CLASS.lstrip(),
        V2_ROOT / "16_route_aware_pandas_prompt_builder.py": pandas_prefix + PANDAS_PROMPT_CLASS.lstrip(),
        V2_ROOT / "18_route_aware_answer_prompt_builder.py": answer_prefix + ANSWER_PROMPT_CLASS.lstrip(),
    }


def main() -> None:
    for path, source in render_sources().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
