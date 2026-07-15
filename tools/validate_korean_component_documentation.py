from __future__ import annotations

import argparse
import ast
import json
import unicodedata
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components"
FLOW_EXPORT_ROOT = ROOT / "flow_exports"
IMPORT_READY_ROOT = ROOT / "import_ready_flows"
IMPORT_ZIP = ROOT / "import_ready_flows.zip"
ENCODING_HEADER = "# -*- coding: utf-8 -*-"
OVERVIEW_MARKER = "# 컴포넌트 개요:"
CUSTOM_MODULE_PREFIXES = ("custom_components.", "v5_auxiliary.")
BROKEN_TEXT_PATTERNS = ("\ufffd", "占쏙옙", "\x00")
FUNCTION_COMMENT_MARKERS = ("# 주요 함수:", "# Langflow 출력 함수:", "# 주요 메서드:", "# 함수 설명:")
EMBEDDED_TEXT_TARGETS = {
    "data_analysis_flow/03_intent_prompt_template_ko.md": (
        "flow_exports/data_analysis_flow_v5_standalone.json",
        "import_ready_flows/01_data_analysis_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "data_analysis_flow/16_pandas_prompt_template_ko.md": (
        "flow_exports/data_analysis_flow_v5_standalone.json",
        "import_ready_flows/01_data_analysis_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "data_analysis_flow/17b_pandas_repair_prompt_template_ko.md": (
        "flow_exports/data_analysis_flow_v5_standalone.json",
        "import_ready_flows/01_data_analysis_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "data_analysis_flow/19_answer_prompt_template_ko.md": (
        "flow_exports/data_analysis_flow_v5_standalone.json",
        "import_ready_flows/01_data_analysis_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "domain_saving_flow/03_saving_prompt_template_ko.md": (
        "flow_exports/domain_saving_flow_v5_standalone.json",
        "import_ready_flows/02_domain_saving_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "table_catalog_saving_flow/03_saving_prompt_template_ko.md": (
        "flow_exports/table_catalog_saving_flow_v5_standalone.json",
        "import_ready_flows/03_table_catalog_saving_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "main_flow_filters_saving_flow/03_saving_prompt_template_ko.md": (
        "flow_exports/main_flow_filter_saving_flow_v5_standalone.json",
        "import_ready_flows/04_main_flow_filter_saving_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "metadata_qa_flow/03_metadata_qa_prompt_template_ko.md": (
        "flow_exports/metadata_qa_flow_v5_standalone.json",
        "import_ready_flows/05_metadata_qa_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "route_flow_v2/SYSTEM_PROMPT_KO.md": (
        "flow_exports/agent_tool_router_flow_v5_standalone.json",
        "import_ready_flows/07_agent_tool_router_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "route_flow_v3/SYSTEM_PROMPT_KO.md": (
        "flow_exports/agent_orchestrator_router_flow_v5_standalone.json",
        "import_ready_flows/08_agent_orchestrator_router_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "route_flow_v4/SYSTEM_PROMPT_KO.md": (
        "flow_exports/workflow_orchestrator_flow_v5_standalone.json",
        "import_ready_flows/09_workflow_orchestrator_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
    "workflow_skill_saving_flow/03_saving_prompt_template_ko.md": (
        "flow_exports/workflow_skill_saving_flow_v5_standalone.json",
        "import_ready_flows/10_workflow_skill_saving_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    ),
}


def _decode_utf8(path: Path) -> tuple[str, list[str]]:
    errors: list[str] = []
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append(f"{path}: UTF-8 BOM이 있습니다")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return "", [f"{path}: strict UTF-8 decode 실패: {exc}"]
    for pattern in BROKEN_TEXT_PATTERNS:
        if pattern in text:
            errors.append(f"{path}: 깨짐 의심 문자열 {pattern!r} 발견")
    if not unicodedata.is_normalized("NFC", text):
        errors.append(f"{path}: Unicode NFC 정규화가 아닙니다")
    return text, errors


def _decorated_start(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    return min([node.lineno, *(item.lineno for item in node.decorator_list)])


def _preceding_comment(lines: list[str], line_number: int, markers: tuple[str, ...]) -> bool:
    index = line_number - 2
    block: list[str] = []
    while index >= 0 and lines[index].lstrip().startswith("#"):
        block.append(lines[index].lstrip())
        index -= 1
    return any(marker in line for line in block for marker in markers)


def _validate_python(path: Path) -> tuple[list[str], int, int]:
    text, errors = _decode_utf8(path)
    if not text:
        return errors, 0, 0
    lines = text.splitlines()
    relative = path.relative_to(ROOT).as_posix()
    if not lines or lines[0] != ENCODING_HEADER:
        errors.append(f"{relative}: 첫 줄 UTF-8 인코딩 선언 누락")
    if OVERVIEW_MARKER not in text:
        errors.append(f"{relative}: 컴포넌트 개요 주석 누락")
    korean_comments = [line for line in lines if line.lstrip().startswith("#") and any("가" <= char <= "힣" for char in line)]
    if len(korean_comments) < 5:
        errors.append(f"{relative}: 한글 설명 주석이 5줄 미만입니다 ({len(korean_comments)})")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        errors.append(f"{relative}: Python AST parse 실패: {exc}")
        return errors, 0, 0

    function_count = 0
    documented_functions = 0
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not _preceding_comment(lines, _decorated_start(node), ("# Langflow 컴포넌트 클래스:", "# 내부 연동 도우미 클래스:")):
            errors.append(f"{relative}:{node.lineno}: 클래스 {node.name} 설명 누락")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        function_count += 1
        if _preceding_comment(lines, _decorated_start(node), FUNCTION_COMMENT_MARKERS):
            documented_functions += 1
        else:
            errors.append(f"{relative}:{node.lineno}: 함수 {node.name} 설명 누락")
    return errors, function_count, documented_functions


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _custom_codes(value: Any) -> list[str]:
    codes: list[str] = []
    for item in _walk(value):
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        template = item.get("template")
        if not isinstance(metadata, dict) or not isinstance(template, dict):
            continue
        module = metadata.get("module")
        if not isinstance(module, str) or not module.startswith(CUSTOM_MODULE_PREFIXES):
            continue
        code = template.get("code", {}).get("value") if isinstance(template.get("code"), dict) else None
        if isinstance(code, str):
            codes.append(code)
    return codes


def _validate_json(path: Path) -> tuple[list[str], Any | None, int, int, int]:
    text, errors = _decode_utf8(path)
    if not text:
        return errors, None, 0, 0, 0
    relative = path.relative_to(ROOT).as_posix()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return [*errors, f"{relative}: JSON parse 실패: {exc}"], None, 0, 0, 0
    codes = _custom_codes(value)
    function_count = 0
    documented_functions = 0
    for index, code in enumerate(codes, start=1):
        if not code.startswith(ENCODING_HEADER + "\n"):
            errors.append(f"{relative}: custom code #{index} UTF-8 선언 누락")
        if OVERVIEW_MARKER not in code:
            errors.append(f"{relative}: custom code #{index} 한글 개요 누락")
        try:
            code_tree = ast.parse(code)
        except SyntaxError as exc:
            errors.append(f"{relative}: custom code #{index} AST parse 실패: {exc}")
            continue
        code_lines = code.splitlines()
        for node in ast.walk(code_tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            function_count += 1
            if _preceding_comment(code_lines, _decorated_start(node), FUNCTION_COMMENT_MARKERS):
                documented_functions += 1
            else:
                errors.append(f"{relative}: custom code #{index} 함수 {node.name} 설명 누락")
    if codes and OVERVIEW_MARKER not in text:
        errors.append(f"{relative}: JSON 원문에 literal 한글 주석이 보존되지 않았습니다")
    return errors, value, len(codes), function_count, documented_functions


def _contains_exact_string(value: Any, expected: str) -> bool:
    return any(isinstance(item, str) and item == expected for item in _walk(value))


def audit() -> dict[str, Any]:
    errors: list[str] = []
    python_paths = sorted(path for path in COMPONENT_ROOT.rglob("*.py") if "__pycache__" not in path.parts)
    python_function_count = 0
    documented_python_functions = 0
    for path in python_paths:
        path_errors, function_count, documented_functions = _validate_python(path)
        errors.extend(path_errors)
        python_function_count += function_count
        documented_python_functions += documented_functions

    component_text_paths = sorted(COMPONENT_ROOT.rglob("*.md"))
    component_texts: dict[str, str] = {}
    for path in component_text_paths:
        text, path_errors = _decode_utf8(path)
        errors.extend(path_errors)
        component_texts[path.relative_to(COMPONENT_ROOT).as_posix()] = text

    json_paths = sorted(FLOW_EXPORT_ROOT.glob("*_v5_standalone.json")) + sorted(IMPORT_READY_ROOT.glob("*.json"))
    parsed: dict[str, Any] = {}
    embedded_count = 0
    embedded_function_count = 0
    documented_embedded_functions = 0
    for path in json_paths:
        path_errors, value, count, function_count, documented_functions = _validate_json(path)
        errors.extend(path_errors)
        embedded_count += count
        embedded_function_count += function_count
        documented_embedded_functions += documented_functions
        if value is not None:
            parsed[path.relative_to(ROOT).as_posix()] = value

    helper_path = COMPONENT_ROOT / "data_analysis_flow" / "function_case_helper_code_input_example.py"
    helper = helper_path.read_text(encoding="utf-8")
    helper_targets = (
        "flow_exports/data_analysis_flow_v5_standalone.json",
        "import_ready_flows/01_data_analysis_flow_v5_standalone.json",
        "import_ready_flows/00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json",
    )
    for target in helper_targets:
        value = parsed.get(target)
        if value is None or not _contains_exact_string(value, helper):
            errors.append(f"{target}: helper library 원본과 exact match하지 않습니다")

    for source, targets in EMBEDDED_TEXT_TARGETS.items():
        # Path.read_text()를 사용하는 빌더는 Windows CRLF를 Python의 universal newline 규칙으로
        # LF로 정규화한다. 원본과 JSON을 같은 기준으로 비교해 줄바꿈 형식 차이를 내용 변경으로
        # 오인하지 않되, BOM과 깨진 문자는 위의 strict decode 검사에서 별도로 차단한다.
        expected = component_texts.get(source, "").replace("\r\n", "\n").replace("\r", "\n")
        if not expected:
            errors.append(f"langflow_components/{source}: 내장 prompt 원본을 읽지 못했습니다")
            continue
        for target in targets:
            value = parsed.get(target)
            if value is None or not _contains_exact_string(value, expected):
                errors.append(f"{target}: langflow_components/{source} 원본과 exact match하지 않습니다")

    zip_entries = 0
    if not IMPORT_ZIP.exists():
        errors.append("import_ready_flows.zip: 파일이 없습니다")
    else:
        with zipfile.ZipFile(IMPORT_ZIP) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                zip_entries += 1
                raw = archive.read(info)
                if raw.startswith(b"\xef\xbb\xbf"):
                    errors.append(f"ZIP/{info.filename}: UTF-8 BOM이 있습니다")
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    errors.append(f"ZIP/{info.filename}: strict UTF-8 decode 실패: {exc}")
                    continue
                if any(pattern in text for pattern in BROKEN_TEXT_PATTERNS):
                    errors.append(f"ZIP/{info.filename}: 깨짐 의심 문자열 발견")
                if info.filename.endswith(".json"):
                    try:
                        json.loads(text)
                    except json.JSONDecodeError as exc:
                        errors.append(f"ZIP/{info.filename}: JSON parse 실패: {exc}")

    return {
        "status": "ok" if not errors else "error",
        "python_files": len(python_paths),
        "python_function_definitions": python_function_count,
        "documented_python_functions": documented_python_functions,
        "component_text_files": len(component_text_paths),
        "embedded_text_sources": len(EMBEDDED_TEXT_TARGETS),
        "json_files": len(json_paths),
        "embedded_custom_code_instances": embedded_count,
        "embedded_function_definitions": embedded_function_count,
        "documented_embedded_functions": documented_embedded_functions,
        "zip_entries": zip_entries,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="한글 컴포넌트 설명과 UTF-8/JSON 내장 코드 무결성을 검증합니다.")
    parser.parse_args()
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
