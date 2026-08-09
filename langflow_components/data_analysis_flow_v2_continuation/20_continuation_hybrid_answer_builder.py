# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 20 V2 Hybrid 답변 생성기
# 역할: Fast 결과는 고정 문장으로 만들고 Complex 결과일 때만 답변 LLM을 지연 호출합니다.
# 주요 입력: 페이로드, Complex 답변 LLM 사용 여부, 답변 prompt, 답변 언어 모델
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: LLM 답변과 결정론적 분석 결과를 합쳐 answer sections, evidence, 현재 상태와 후속 상태를 구성합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from math import isclose
from copy import deepcopy
from time import perf_counter
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, MessageTextInput, ModelInput, MultilineInput, Output, SecretStrInput
from lfx.schema.data import Data

RUNTIME_BUFFER_KEYS = {
    "runtime_sources",
    "_runtime_rows_by_alias",
    "_full_result_rows",
    "_runtime_result_rows",
    "_intermediate_download_rows",
    "_intermediate_download_metadata",
}

ANSWER_EVIDENCE_ROW_LIMIT = 5
ANSWER_EVIDENCE_COLUMN_LIMIT = 16
ANSWER_EVIDENCE_CELL_LIMIT = 160
ANSWER_EVIDENCE_ITEM_LIMIT = 4
ANSWER_EVIDENCE_MESSAGE_LIMIT = 600
ANSWER_PROMPT_REFERENCE_REWRITES = {
    "answer_context_json.applied_criteria": "applied_scope_json.criteria",
    "answer_context_json.result_shape.columns": "result_summary_json.columns",
}


# 주요 함수: LLM 문장과 분석 결과를 합쳐 최종 구조화 답변과 다음 상태를 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_answer_response(payload_value: Any, answer_text: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    received_structured_answer = _answer_payload(answer_text)
    received_message = (
        _answer_text_from_dict(received_structured_answer)
        if received_structured_answer
        else _answer_text(answer_text)
    ).strip()
    blocked = _execution_blocked(payload)
    structured_answer = {} if blocked else received_structured_answer
    message = str(payload.get("answer_message") or "").strip() if blocked else received_message
    if not message:
        message = str(payload.get("answer_message") or "").strip()
    if not message:
        row_count = payload.get("data", {}).get("row_count", 0)
        message = f"분석 결과 {row_count}건을 확인했습니다." if payload.get("analysis", {}).get("status") == "ok" else "분석을 완료하지 못했습니다. trace의 오류를 확인해 주세요."
    next_payload = payload
    next_payload.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"] = {
        "stage": "20_answer_response_builder",
        "received": bool(received_message or received_structured_answer),
        "used": not blocked and bool(received_message or received_structured_answer),
        "ignored": blocked and bool(received_message or received_structured_answer),
        "policy": "ignore" if blocked else "use",
    }
    message, grounding = _ground_answer_message(next_payload, message)
    if grounding:
        trace = next_payload.setdefault("trace", {})
        trace.setdefault("warnings", []).append(
            {
                "type": "answer_value_grounded",
                "message": "LLM 답변의 수치가 실제 결과 행과 일치하지 않아 결과 행 기준 문장으로 교정했습니다.",
            }
        )
        trace.setdefault("inspection", {})["answer_grounding"] = grounding
    next_payload["answer_message"] = message
    next_payload["answer_sections"] = _build_answer_sections(next_payload, message, _dict(structured_answer.get("answer_sections")))
    next_payload["state"] = _build_next_turn_state(next_payload)
    return next_payload


# 함수 설명: `_execution_blocked()`는 필수 조회 실패 시 기본 Language Model 응답을 최종 답변에 사용하지 않도록 판정합니다.
def _execution_blocked(payload: dict[str, Any]) -> bool:
    gate = _dict(payload.get("execution_gate"))
    return str(gate.get("status") or "").strip().lower() == "blocked"


# 함수 설명: `_ground_answer_message()`는 LLM 문장에 결과 행으로 확인되지 않는 수치가 있을 때 재호출 없이 결정론적으로 교정합니다.
def _ground_answer_message(payload: dict[str, Any], message: str) -> tuple[str, dict[str, Any]]:
    analysis = _dict(payload.get("analysis"))
    data = _dict(payload.get("data"))
    rows = _list(data.get("rows"))
    if analysis.get("status") != "ok" or not rows or not message:
        return message, {}

    for operation in ("compare_presence", "compare_metrics"):
        certificate = _verified_semantic_execution_certificate(payload, operation)
        if not certificate:
            continue
        grounded_message = _authoritative_result_message(payload)
        if grounded_message:
            return grounded_message, {
                "stage": "20_answer_response_builder",
                "status": "verified_semantic_contract",
                "operation": operation,
                "policy": "semantic_execution_certificate",
            }

    unsupported = _unsupported_numeric_claims(payload, message)
    if not unsupported:
        return message, {}

    grounded_message = _authoritative_result_message(payload)
    if not grounded_message:
        return message, {}
    return grounded_message, {
        "stage": "20_answer_response_builder",
        "status": "corrected",
        "unsupported_numeric_claims": unsupported,
        "policy": "deterministic_data_rows",
    }


# 함수 설명: `_verified_semantic_execution_certificate()`는 20 답변 응답 생성기 처리 중 semantic·execution·certificate 관련 값을
#        계산·변환하는 내부 helper입니다.
def _verified_semantic_execution_certificate(
    payload: dict[str, Any],
    operation: str,
) -> dict[str, Any]:
    analysis = _dict(payload.get("analysis"))
    certificate = _dict(analysis.get("semantic_execution_certificate"))
    if (
        str(certificate.get("operation") or "").strip() == operation
        and str(certificate.get("postcondition_validation") or "").strip() == "passed"
    ):
        return certificate
    return {}


# 함수 설명: `_unsupported_numeric_claims()`는 질문 조건이나 실제 결과에 없는 LLM 수치 주장만 선별합니다.
def _unsupported_numeric_claims(payload: dict[str, Any], message: str) -> list[str]:
    known_values = _known_numeric_values(payload)
    unsupported: list[str] = []
    for raw, number in _numeric_claims(message):
        claim_values = [number, number * 100] if raw.endswith("%") else [number]
        if any(
            isclose(claim, known, rel_tol=1e-9, abs_tol=1e-9)
            for claim in claim_values
            for known in known_values
        ):
            continue
        unsupported.append(raw)
    return unsupported


# 함수 설명: `_known_numeric_values()`는 결과 행과 질문 조건에서 답변에 나타나도 되는 수치 집합을 구성합니다.
def _known_numeric_values(payload: dict[str, Any]) -> list[float]:
    data = _dict(payload.get("data"))
    values: list[float] = []
    for row in _list(data.get("rows")):
        if not isinstance(row, dict):
            continue
        for value in row.values():
            values.extend(_numbers_from_value(value))
    values.extend(_numbers_from_value(data.get("row_count")))
    values.extend(_numbers_from_value(_dict(payload.get("request"))))
    values.extend(_numbers_from_value(_dict(payload.get("intent_plan")).get("retrieval_jobs")))
    return _dedupe_numbers(values)


# 함수 설명: `_numbers_from_value()`는 숫자와 날짜형 문자열을 재귀적으로 읽어 비교 가능한 실수 값으로 바꿉니다.
def _numbers_from_value(value: Any) -> list[float]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except Exception:
            return []
        return [] if number != number else [number]
    if isinstance(value, dict):
        return [number for item in value.values() for number in _numbers_from_value(item)]
    if isinstance(value, (list, tuple, set)):
        return [number for item in value for number in _numbers_from_value(item)]
    text = str(value or "").strip()
    if not text:
        return []
    numbers = [number for _, number in _numeric_claims(text)]
    for date_text in re.findall(r"(?<!\d)(\d{8})(?!\d)", text):
        numbers.extend([float(date_text[:4]), float(date_text[4:6]), float(date_text[6:8])])
    return numbers


# 함수 설명: `_numeric_claims()`는 제품 코드 안의 숫자는 제외하고 일반 수치·K/M 단위·퍼센트 표현을 파싱합니다.
def _numeric_claims(text: str) -> list[tuple[str, float]]:
    pattern = re.compile(r"(?<![A-Za-z0-9_/.-])([+-]?\d[\d,]*(?:\.\d+)?)([KkMm]?)(%)?(?![A-Za-z0-9_/.-])")
    claims: list[tuple[str, float]] = []
    for match in pattern.finditer(str(text or "")):
        raw = match.group(0).strip()
        try:
            number = float(match.group(1).replace(",", ""))
        except Exception:
            continue
        unit = match.group(2).lower()
        if unit == "k":
            number *= 1000
        elif unit == "m":
            number *= 1_000_000
        if match.group(3):
            number /= 100
        claims.append((raw, number))
    return claims


# 함수 설명: `_authoritative_result_message()`는 실제 data.rows와 사용자 질문만 이용해 수치 모순이 없는 질문 맞춤형 교정 문장을 만듭니다.
def _authoritative_result_message(payload: dict[str, Any]) -> str:
    data = _dict(payload.get("data"))
    rows = [row for row in _list(data.get("rows")) if isinstance(row, dict)]
    if not rows:
        return ""
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    first_row = rows[0]
    row_count = _int(data.get("row_count"), len(rows))
    question = str(_dict(payload.get("request")).get("question") or "").strip()
    metric_column = _primary_metric_column(payload, columns, rows, question)
    subject = _result_subject(payload, first_row, columns, metric_column)
    presence_message = _presence_comparison_message(payload, row_count, columns, metric_column, question)
    if presence_message:
        return presence_message
    metric_comparison_message = _metric_comparison_message(
        payload,
        row_count,
        columns,
        metric_column,
        question,
    )
    if metric_comparison_message:
        return metric_comparison_message

    if metric_column and metric_column in first_row:
        metric_label = _metric_label(payload, metric_column)
        metric_value = _display_value(first_row.get(metric_column))
        segmented_message = _segmented_result_message(
            payload,
            rows,
            columns,
            metric_column,
            metric_label,
        )
        if segmented_message:
            return segmented_message
        if _is_ranking_question(question) or _output_ordering(payload):
            requested_count = _requested_result_count(payload)
            is_lowest = _is_lowest_ranking(question) or str(_output_ordering(payload).get("order")) == "asc"
            rank_label = "하위" if is_lowest else "상위"
            if requested_count and row_count < requested_count:
                intro = f"{rank_label} {requested_count}개를 요청했으며, 조건에 맞는 결과는 {row_count:,}건입니다."
            else:
                intro = f"조건에 맞는 순위 결과는 총 {row_count:,}건입니다."
            direction = "가장 적은" if is_lowest else "가장 많은"
            metric_sentence = (
                f"이 중 {metric_label}이 {direction} 대상은 {subject}이며, "
                f"{metric_label}은 {metric_value}입니다."
                if subject
                else f"이 중 {metric_label}이 {direction} 첫 결과는 {metric_value}입니다."
            )
            details = [intro, metric_sentence]
            details.append("나머지 대상별 상세 결과는 아래 결과 표에서 확인할 수 있습니다.")
            return "\n\n".join(details)

        if _should_compare_extremes(payload, question):
            comparison_message = _multi_row_comparison_message(
                payload,
                rows,
                columns,
                metric_column,
                row_count,
                question,
            )
            if comparison_message:
                return comparison_message

        metric_sentence = (
            f"{subject}의 {metric_label}은 {metric_value}입니다."
            if subject
            else f"{metric_label}은 {metric_value}입니다."
        )
        details = [metric_sentence]
        if row_count > 1:
            details.append(f"전체 결과는 총 {row_count:,}건이며, 상세 값은 아래 결과 표에서 확인할 수 있습니다.")
        return "\n\n".join(details)

    facts = []
    for column in columns[:6]:
        if column not in first_row:
            continue
        value = _display_value(first_row.get(column))
        facts.append(f"{column}={value}")
    if row_count <= 1:
        return f"분석 결과 {', '.join(facts)}입니다." if facts else "분석 결과 1건입니다."
    prefix = f"분석 결과 총 {row_count:,}건입니다."
    return f"{prefix} 첫 번째 결과는 {', '.join(facts)}입니다." if facts else prefix


# 함수 설명: `_multi_row_comparison_message()`는 다건 결과 전체에서 대표 지표의 최댓값·최솟값을 찾아 첫 행 편향 없는 교정 문장을 만듭니다.
def _multi_row_comparison_message(
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
    metric_column: str,
    row_count: int,
    question: str,
) -> str:
    numeric_rows: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        number = _finite_number(row.get(metric_column))
        if number is not None:
            numeric_rows.append((row, number))
    if len(numeric_rows) < 2:
        return ""

    highest_row, highest_value = max(numeric_rows, key=lambda item: item[1])
    lowest_row, lowest_value = min(numeric_rows, key=lambda item: item[1])
    metric_label = _metric_label(payload, metric_column)
    dimension_label = _comparison_dimension_label(payload, columns, metric_column, question)
    highest_subject = _comparison_subject(payload, highest_row, columns, metric_column, dimension_label)
    lowest_subject = _comparison_subject(payload, lowest_row, columns, metric_column, dimension_label)
    if not highest_subject or not lowest_subject:
        return ""

    intro = (
        f"같은 조건의 {dimension_label}별 {metric_label} 결과는 총 {row_count:,}건입니다."
        if dimension_label != "대상"
        else f"같은 조건의 {metric_label} 비교 결과는 총 {row_count:,}건입니다."
    )
    highest_word, lowest_word = _metric_extreme_words(metric_column)
    if isclose(highest_value, lowest_value, rel_tol=1e-12, abs_tol=1e-12):
        comparison = (
            f"확인된 {dimension_label}의 {metric_label}은 모두 "
            f"{_display_value(highest_row.get(metric_column))}로 같습니다."
        )
    else:
        comparison = (
            f"{metric_label}이 가장 {highest_word} {dimension_label}은 "
            f"{highest_subject}({_display_value(highest_row.get(metric_column))})이고, "
            f"가장 {lowest_word} {dimension_label}은 "
            f"{lowest_subject}({_display_value(lowest_row.get(metric_column))})입니다."
        )
    detail_label = f"{dimension_label}별 " if dimension_label != "대상" else ""
    return "\n\n".join(
        [
            intro,
            comparison,
            f"나머지 {detail_label}상세 값은 아래 결과 표에서 확인할 수 있습니다.",
        ]
    )


# 함수 설명: `_comparison_dimension_label()`은 질문 표현과 결과 컬럼을 사용해 공정·제품·장비·LOT·기준일 등 비교 대상 이름을 결정합니다.
def _comparison_dimension_label(
    payload: dict[str, Any],
    columns: list[str],
    metric_column: str,
    question: str,
) -> str:
    resolved_grain = _dict(_dict(payload.get("intent_plan")).get("resolved_grain_plan"))
    grain_columns = _string_list(resolved_grain.get("grain_columns"))
    candidates = grain_columns or [column for column in columns if column != metric_column]
    normalized = {str(column or "").upper().replace(" ", "_") for column in candidates}
    if normalized.intersection({"OPER_NAME", "OPER_NM", "OPER", "PROCESS", "공정"}):
        return "공정"
    if normalized.intersection({"EQUIP_ID", "EQP_ID", "EQUIPMENT_ID", "장비"}):
        return "장비"
    if normalized.intersection({"LOT_ID", "LOT", "랏"}):
        return "LOT"
    if normalized.intersection({"WORK_DATE", "WORK_DT", "DATE", "BASE_DT", "기준일", "일자"}):
        return "기준일"
    product_columns = {"TECH", "DENSITY", "DEN", "MODE", "ORG", "PKG1", "PKG2", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"}
    if len(normalized.intersection(product_columns)) >= 2:
        return "제품"
    lowered = str(question or "").lower()
    question_labels = (
        (("제품", "product", "device"), "제품"),
        (("장비", "equipment", "eqp"), "장비"),
        (("lot", "랏"), "LOT"),
        (("공정", "oper"), "공정"),
        (("일자", "날짜", "기준일", "date"), "기준일"),
    )
    for tokens, label in question_labels:
        if any(token in lowered for token in tokens):
            return label
    return "대상"


# 함수 설명: `_comparison_subject()`는 비교 대상 유형에 맞는 행 식별값을 골라 자연스러운 대표 대상 문자열로 만듭니다.
def _comparison_subject(
    payload: dict[str, Any],
    row: dict[str, Any],
    columns: list[str],
    metric_column: str,
    dimension_label: str,
) -> str:
    preferred_by_label = {
        "공정": ["OPER_NAME", "OPER_NM", "OPER", "공정"],
        "장비": ["EQUIP_ID", "EQP_ID", "EQUIPMENT_ID", "장비"],
        "LOT": ["LOT_ID", "LOT", "랏"],
        "기준일": ["WORK_DATE", "WORK_DT", "DATE", "BASE_DT", "기준일", "일자"],
    }
    for column in preferred_by_label.get(dimension_label, []):
        if column not in row:
            continue
        value = _natural_text(row.get(column))
        if value:
            return value
    return _result_subject(payload, row, columns, metric_column)


# 함수 설명: `_metric_extreme_words()`는 수량 계열 지표와 비율·성능 지표에 맞는 최댓값·최솟값 표현을 반환합니다.
def _metric_extreme_words(metric_column: str) -> tuple[str, str]:
    upper = str(metric_column or "").upper()
    quantity_tokens = ("WIP", "PRODUCTION", "OUTPUT", "QTY", "COUNT", "수량", "생산", "재공", "건수")
    return ("많은", "적은") if any(token in upper for token in quantity_tokens) else ("높은", "낮은")


# 함수 설명: `_finite_number()`는 결과 지표 값을 비교 가능한 유한 실수로 변환하고 비수치·결측값은 제외합니다.
def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return None if number != number else number


# 함수 설명: `_natural_text()`는 배열형 차원 값을 JSON 표기 대신 쉼표로 연결한 자연스러운 대상 문자열로 변환합니다.
def _natural_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(text for text in (_natural_text(item) for item in value) if text)
    if value is None:
        return ""
    return str(value).strip()


# 함수 설명: 결과 계약과 질문 표현을 함께 사용해 답변의 대표 지표 컬럼을 선택합니다.
def _primary_metric_column(
    payload: dict[str, Any],
    columns: list[str],
    rows: list[dict[str, Any]],
    question: str,
) -> str:
    output_contract = _dict(_dict(payload.get("intent_plan")).get("output_contract"))
    ordering = _dict(output_contract.get("ordering"))
    explicit = _string_list(
        [
            ordering.get("sort_by"),
            output_contract.get("primary_metric"),
            *_string_list(output_contract.get("metric_columns")),
        ]
    )
    for column in explicit:
        if column in columns:
            return column

    lowered = question.lower()
    preferred_tokens = []
    if "재공" in lowered or "wip" in lowered:
        preferred_tokens.extend(("WIP", "BOH", "EOH"))
    if "생산" in lowered or "실적" in lowered or "production" in lowered:
        preferred_tokens.extend(("PRODUCTION", "OUTPUT"))
    if "uph" in lowered:
        preferred_tokens.append("UPH")
    if "달성" in lowered or "비율" in lowered or "rate" in lowered:
        preferred_tokens.extend(("RATE", "RATIO", "달성률"))
    preferred_tokens.extend(("WIP", "PRODUCTION", "QTY", "COUNT", "UPH", "RATE", "RATIO", "PLAN", "실적", "수량"))

    for token in preferred_tokens:
        token_upper = token.upper()
        for column in columns:
            if token_upper in column.upper() and _column_has_numeric_value(rows, column):
                return column
    return ""


# 함수 설명: 존재/부재 비교 결과를 일반 최대·최소 집계로 오해하지 않고 비교 조건에 맞는 건수 문장으로 설명합니다.
def _presence_comparison_message(
    payload: dict[str, Any],
    row_count: int,
    columns: list[str],
    metric_column: str,
    question: str,
) -> str:
    if not _verified_semantic_execution_certificate(payload, "compare_presence"):
        return ""
    dimension_label = _comparison_dimension_label(payload, columns, metric_column, question)
    target = f"{dimension_label}은" if dimension_label != "대상" else "대상은"
    return "\n\n".join(
        [
            f"요청한 존재·부재 조건을 만족한 {target} 총 {row_count:,}건입니다.",
            "왼쪽 기준에는 값이 존재하고 오른쪽 기준에는 값이 없거나 0인 결과만 아래 표에 표시했습니다.",
        ]
    )


# 함수 설명: 검증된 수치 metric 비교 결과를 부등식 방향과 결과 건수 중심의 결정론적 문장으로 설명합니다.
def _metric_comparison_message(
    payload: dict[str, Any],
    row_count: int,
    columns: list[str],
    metric_column: str,
    question: str,
) -> str:
    certificate = _verified_semantic_execution_certificate(payload, "compare_metrics")
    if not certificate:
        return ""
    lhs_column = str(certificate.get("lhs_metric_column") or "").strip()
    rhs_column = str(certificate.get("rhs_metric_column") or "").strip()
    operator = str(certificate.get("operator") or "").strip().lower()
    if not lhs_column or not rhs_column:
        return ""
    symbols = {"gt": ">", "ge": ">=", "lt": "<", "le": "<=", "eq": "=", "ne": "!="}
    symbol = symbols.get(operator)
    if not symbol:
        return ""
    lhs_label = _metric_label(payload, lhs_column)
    rhs_label = _metric_label(payload, rhs_column)
    dimension_label = _comparison_dimension_label(
        payload,
        columns,
        metric_column or lhs_column,
        question,
    )
    target = f"{dimension_label}은" if dimension_label != "대상" else "대상은"
    return "\n\n".join(
        [
            f"요청한 {rhs_label} 대비 {lhs_label} 비교 조건을 만족한 {target} 총 {row_count:,}건입니다.",
            f"{lhs_label} {symbol} {rhs_label} 조건을 통과한 결과만 아래 표에 표시했습니다.",
        ]
    )


# 함수 설명: 사용자가 실제 최대·최소 비교를 요청했을 때만 다건 극값 요약을 허용합니다.
def _should_compare_extremes(payload: dict[str, Any], question: str) -> bool:
    if _output_ordering(payload):
        return True
    lowered = str(question or "").lower()
    return (
        ("다른" in lowered and any(token in lowered for token in ("어때", "어떻", "비교")))
        or any(
        token in lowered
        for token in ("비교", "가장", "상위", "하위", "많은", "적은", "높은", "낮은", "최대", "최소")
        )
    )


# 함수 설명: output contract의 단일 정렬 계약을 안전한 dict로 반환합니다.
def _output_ordering(payload: dict[str, Any]) -> dict[str, Any]:
    contract = _dict(_dict(payload.get("intent_plan")).get("output_contract"))
    return _dict(contract.get("ordering"))


# 함수 설명: metadata grain 또는 첫 dimension 값을 key=value 나열 대신 사람이 읽을 대표 대상 표현으로 묶습니다.
def _result_subject(
    payload: dict[str, Any],
    row: dict[str, Any],
    columns: list[str],
    metric_column: str,
) -> str:
    intent_plan = _dict(payload.get("intent_plan"))
    resolved_grain = _dict(intent_plan.get("resolved_grain_plan"))
    output_contract = _dict(intent_plan.get("output_contract"))
    grain_columns = (
        _string_list(resolved_grain.get("grain_columns"))
        or _string_list(output_contract.get("grain_columns"))
    )
    values: list[str] = []
    for column in grain_columns:
        if column not in columns:
            continue
        value = str(row.get(column) or "").strip()
        if value and value not in values:
            values.append(value)
    if values:
        return " ".join(values)

    dimension_columns = [
        column
        for column in columns
        if column != metric_column and not _column_has_numeric_value([row], column)
    ]
    if not dimension_columns:
        return ""
    column = dimension_columns[0]
    value = str(row.get(column) or "").strip()
    if not value:
        return ""
    suffix = {
        "OPER_NAME": "공정",
        "OPER_NM": "공정",
        "DEVICE": "제품",
        "LOT_ID": "LOT",
        "EQUIP_ID": "장비",
        "EQP_ID": "장비",
    }.get(column, "")
    return f"{value} {suffix}".strip()


# 함수 설명: 질문이 상위·하위 또는 최댓값·최솟값 순위 요청인지 판정합니다.
def _is_ranking_question(question: str) -> bool:
    lowered = str(question or "").lower()
    return any(token in lowered for token in ("상위", "하위", "가장 많", "가장 적", "top", "bottom"))


# 함수 설명: 순위 질문이 오름차순 최솟값 방향인지 판정합니다.
def _is_lowest_ranking(question: str) -> bool:
    lowered = str(question or "").lower()
    return any(token in lowered for token in ("하위", "가장 적", "bottom"))


# 함수 설명: RESULT_GROUP과 RESULT_RANK가 있는 복수 결과를 구간별로 나누어 대표 대상과 실제 건수를 설명합니다.
def _segmented_result_message(
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
    metric_column: str,
    metric_label: str,
) -> str:
    contract = _dict(_dict(payload.get("intent_plan")).get("output_contract"))
    segment_column = str(contract.get("segment_column") or "RESULT_GROUP").strip()
    rank_column = str(contract.get("rank_column") or "").strip()
    if segment_column not in columns:
        return ""

    segment_order = [
        str(item.get("label") or "").strip()
        for item in _list(contract.get("result_segments"))
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        label = str(row.get(segment_column) or "").strip()
        if not label:
            continue
        grouped.setdefault(label, []).append(row)
        if label not in segment_order:
            segment_order.append(label)
    if len(grouped) < 2:
        return ""

    details = [f"{metric_label} 기준 결과를 {len(grouped):,}개 구간으로 나누어 조회했습니다."]
    entity_signatures: dict[str, set[str]] = {}
    for label in segment_order:
        segment_rows = grouped.get(label, [])
        if not segment_rows:
            continue
        ordered_rows = (
            sorted(segment_rows, key=lambda row: _rank_sort_value(row.get(rank_column)))
            if rank_column and rank_column in columns
            else segment_rows
        )
        first = ordered_rows[0]
        subject = _result_subject(payload, first, columns, metric_column)
        value = _display_value(first.get(metric_column))
        lead = f"{subject}, {metric_label} {value}" if subject else f"{metric_label} {value}"
        if rank_column and rank_column in columns:
            details.append(f"- {label}: {len(ordered_rows):,}건이며 1위는 {lead}입니다.")
        else:
            details.append(f"- {label}: {len(ordered_rows):,}건입니다.")
        entity_signatures[label] = {
            _row_entity_signature(row, columns, metric_column, segment_column, rank_column)
            for row in ordered_rows
        }

    non_empty_signatures = [values for values in entity_signatures.values() if values]
    if len(non_empty_signatures) >= 2 and set.intersection(*non_empty_signatures):
        details.append("전체 후보 수가 요청 범위보다 적어 일부 대상이 서로 다른 결과 구간에 함께 포함되었습니다.")
    details.append("구간 구분과 구간 내 순위는 아래 결과 표에서 확인할 수 있습니다.")
    return "\n".join(details)


# 함수 설명: 구간 내 순위 값이 숫자면 숫자 순서로, 아니면 마지막 순서로 안정 정렬합니다.
def _rank_sort_value(value: Any) -> tuple[int, float | str]:
    try:
        return (0, float(value))
    except Exception:
        return (1, str(value or ""))


# 함수 설명: 결과 구간 간 같은 대상이 겹치는지 비교할 수 있도록 지표·표시 컬럼을 제외한 행 식별 문자열을 만듭니다.
def _row_entity_signature(
    row: dict[str, Any],
    columns: list[str],
    metric_column: str,
    segment_column: str,
    rank_column: str,
) -> str:
    excluded = {metric_column, segment_column}
    if rank_column:
        excluded.add(rank_column)
    values = [
        f"{column}={row.get(column)}"
        for column in columns
        if column not in excluded and row.get(column) not in (None, "")
    ]
    return "|".join(values)


# 함수 설명: intent output contract 또는 질문에서 요청한 순위 건수를 추출합니다.
def _requested_result_count(payload: dict[str, Any]) -> int:
    output_contract = _dict(_dict(payload.get("intent_plan")).get("output_contract"))
    ordering_limit = _int(_dict(output_contract.get("ordering")).get("limit"), 0)
    if ordering_limit > 0:
        return ordering_limit
    for key in ("top_n", "bottom_n", "limit"):
        count = _int(output_contract.get(key), 0)
        if count > 0:
            return count
    question = str(_dict(payload.get("request")).get("question") or "")
    match = re.search(r"(\d+)\s*개", question)
    return _int(match.group(1), 0) if match else 0


# 함수 설명: output contract의 문맥 표시명을 우선 사용하고 내부 지표 컬럼명을 자연어 지표명으로 변환합니다.
def _metric_label(payload: dict[str, Any], column: str) -> str:
    labels = _dict(_dict(_dict(payload.get("intent_plan")).get("output_contract")).get("column_labels"))
    explicit = str(labels.get(column) or "").strip()
    if explicit:
        return explicit
    upper = str(column or "").upper()
    if "TOTAL" in upper and ("QUANTITY" in upper or "QTY" in upper or "수량" in column):
        return "합계 수량"
    if "WIP" in upper:
        return "재공 수량"
    if "PRODUCTION" in upper or "OUTPUT" in upper:
        return "생산량"
    if "UPH" in upper:
        return "UPH"
    if "RATE" in upper or "RATIO" in upper or "달성률" in column:
        return "달성률"
    if "COUNT" in upper:
        return "건수"
    if "QTY" in upper or "수량" in column:
        return "수량"
    return str(column)


# 함수 설명: `_dedupe_numbers()`는 부동소수 비교 오차를 고려해 숫자 목록의 중복을 제거합니다.
def _dedupe_numbers(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if not any(isclose(value, existing, rel_tol=1e-12, abs_tol=1e-12) for existing in result):
            result.append(value)
    return result


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return {}
    payload = {
        key: deepcopy(item)
        for key, item in data.items()
        if key not in RUNTIME_BUFFER_KEYS
    }
    for key in RUNTIME_BUFFER_KEYS:
        if key in data:
            payload[key] = data[key]
    return payload


# 함수 설명: `_answer_text()`는 문자열에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _answer_text(value: Any) -> str:
    if isinstance(value, dict):
        text = _answer_text_from_dict(value)
        return text if text else json.dumps(value, ensure_ascii=False, default=str)

    text = _message_text(value).strip()
    parsed = _json_text(text)
    if parsed:
        parsed_text = _answer_text_from_dict(parsed)
        if parsed_text:
            return parsed_text
    return text


# 함수 설명: `_answer_payload()`는 페이로드에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _answer_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return deepcopy(data)
    text = _message_text(value).strip()
    return _json_text(text)


# 함수 설명: `_answer_text_from_dict()`는 문자열·원본·DICT에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _answer_text_from_dict(value: dict[str, Any]) -> str:
    for key in ("answer_message", "answer", "text", "message", "output"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    data = value.get("data")
    if isinstance(data, dict):
        return _answer_text_from_dict(data)
    return ""


# 함수 설명: `_message_text()`는 문자열에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _message_text(value: Any) -> str:
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str):
            return text
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        extracted = _answer_text_from_dict(data)
        if extracted:
            return extracted
    return str(value or "")


# 함수 설명: `_json_text()`는 LLM 답변에서 Markdown fence를 제거하고 JSON object 문자열만 추출합니다.
def _json_text(text: str) -> dict[str, Any]:
    if not text:
        return {}
    candidate = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
    elif "{" in candidate and "}" in candidate:
        candidate = candidate[candidate.find("{") : candidate.rfind("}") + 1]
    try:
        parsed = json.loads(candidate)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# 함수 설명: `_build_answer_sections()`는 답변·응답 section 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def _build_answer_sections(payload: dict[str, Any], answer_message: str, section_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _dict(payload.get("data"))
    overrides = _dict(section_overrides)
    result_table_overrides = _dict(overrides.get("result_table"))
    rows = _list(data.get("rows"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    display_columns = _string_list(data.get("display_columns")) or _string_list(result_table_overrides.get("display_columns"))
    override_labels = _dict(result_table_overrides.get("column_labels"))
    default_labels = _result_contract_column_labels(payload, columns)
    column_labels = {**default_labels, **override_labels, **_dict(data.get("column_labels"))}
    if not display_columns and default_labels:
        priority_columns = [column for column in ("RESULT_GROUP", "RESULT_RANK") if column in columns]
        display_columns = priority_columns + [column for column in columns if column not in priority_columns]
    row_count = _int(data.get("row_count"), len(rows))
    applied_criteria = _applied_criteria(payload)
    evidence = _evidence(payload)
    notices = _notices(payload, row_count, rows)
    downloads = _downloads(payload)
    return {
        "summary": {
            "headline": answer_message,
            "basis": _summary_basis(applied_criteria),
        },
        "result_table": _omit_empty(
            {
                "columns": columns,
                "display_columns": display_columns,
                "column_labels": deepcopy(column_labels),
                "row_source": "data.rows",
                "row_count": row_count,
            }
        ),
        "applied_criteria": applied_criteria,
        "evidence": evidence,
        "notices": notices,
        "downloads": downloads,
        "next_questions": _next_questions(payload),
    }


# 함수 설명: 공통 결과 구간 컬럼을 사용자가 이해하기 쉬운 표 머리글로 표시합니다.
def _result_contract_column_labels(payload: dict[str, Any], columns: list[str]) -> dict[str, str]:
    contract = _dict(_dict(payload.get("intent_plan")).get("output_contract"))
    segment_column = str(contract.get("segment_column") or "RESULT_GROUP").strip()
    rank_column = str(contract.get("rank_column") or "RESULT_RANK").strip()
    labels: dict[str, str] = {
        column: str(label).strip()
        for column, label in _dict(contract.get("column_labels")).items()
        if column in columns and str(label).strip()
    }
    if segment_column in columns:
        labels[segment_column] = "구분"
    if rank_column in columns:
        labels[rank_column] = "구간 내 순위"
    return labels


# 함수 설명: `_applied_criteria()`는 조회 작업과 pandas 계획에서 실제 적용된 날짜·제품·공정·지표 조건을 구성합니다.
def _applied_criteria(payload: dict[str, Any]) -> dict[str, Any]:
    plan = _dict(payload.get("intent_plan"))
    retrieval_jobs = _list(plan.get("retrieval_jobs"))
    pandas_plan = _list(plan.get("pandas_execution_plan"))
    source_results = _list(payload.get("source_results"))
    required_params: dict[str, Any] = {}
    analysis_filters: dict[str, Any] = {}
    retrieval_filters: dict[str, Any] = {}
    datasets: list[dict[str, Any]] = []
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        dataset_key = str(job.get("dataset_key") or "").strip()
        source_type = str(job.get("source_type") or "").strip()
        if dataset_key or alias:
            datasets.append(_omit_empty({"dataset_key": dataset_key, "source_alias": alias, "source_type": source_type}))
        params = _dict(job.get("required_params"))
        if params:
            required_params[alias or dataset_key or f"job_{len(required_params) + 1}"] = deepcopy(params)
        filters = _dict(job.get("filters"))
        if filters:
            analysis_filters[alias or dataset_key or f"job_{len(analysis_filters) + 1}"] = deepcopy(filters)
    for source in source_results:
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        dataset_key = str(source.get("dataset_key") or "").strip()
        source_type = str(source.get("source_type") or "").strip()
        if dataset_key or alias:
            datasets.append(_omit_empty({"dataset_key": dataset_key, "source_alias": alias, "source_type": source_type, "row_count": source.get("row_count")}))
        params = _dict(source.get("applied_params"))
        if params:
            required_params[alias or dataset_key or f"source_{len(required_params) + 1}"] = deepcopy(params)
        pandas_filters = _dict(source.get("pandas_filters")) or _dict(source.get("applied_filters"))
        if pandas_filters:
            analysis_filters[alias or dataset_key or f"source_{len(analysis_filters) + 1}"] = deepcopy(pandas_filters)
        retriever_filters = _dict(_dict(source.get("source_execution")).get("filters_applied_in_retriever"))
        if retriever_filters:
            retrieval_filters[alias or dataset_key or f"source_{len(retrieval_filters) + 1}"] = deepcopy(retriever_filters)
    condition_resolution = _dict(plan.get("condition_resolution"))
    effective_filters = _dict(condition_resolution.get("effective_filters"))
    for alias, item in effective_filters.items():
        if not isinstance(item, dict):
            continue
        filters = item.get("filters")
        if filters not in (None, "", [], {}):
            analysis_filters[str(alias)] = deepcopy(filters)
    return _omit_empty(
        {
            "required_params": required_params,
            "analysis_filters": analysis_filters,
            "retrieval_filters": retrieval_filters,
            "group_by": _group_by_columns(pandas_plan),
            "metrics": _metric_columns(payload),
            "datasets": _dedupe_dicts(datasets),
        }
    )


# 함수 설명: `_evidence()`는 조회·pandas 실행 trace에서 답변 수치의 데이터셋과 조건 근거를 구성합니다.
def _evidence(payload: dict[str, Any]) -> dict[str, Any]:
    analysis = _dict(payload.get("analysis"))
    pandas_execution = _dict(_dict(_dict(payload.get("trace")).get("inspection")).get("pandas_execution"))
    step_outputs = _list(analysis.get("step_outputs")) or _list(pandas_execution.get("step_outputs"))
    function_case_results = _list(analysis.get("function_case_results")) or _list(pandas_execution.get("function_case_results"))
    intermediate_results = _list(payload.get("intermediate_results")) or _list(analysis.get("intermediate_results")) or _list(pandas_execution.get("intermediate_results"))
    return _omit_empty(
        {
            "datasets": _compact_source_results(_list(payload.get("source_results"))),
            "calculation_rules": deepcopy(_list(payload.get("metadata_refs")))[:10],
            "step_outputs": deepcopy(step_outputs[:6]),
            "function_case_results": deepcopy(function_case_results[:6]),
            "intermediate_results": deepcopy(intermediate_results[:8]),
        }
    )


# 함수 설명: `_notices()`는 warnings와 errors를 사용자에게 보여 줄 중복 없는 안내 목록으로 정리합니다.
def _notices(payload: dict[str, Any], row_count: int, rows: list[Any]) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    if row_count == 0 and not rows:
        notices.append({"type": "empty_result", "message": "조건에 맞는 결과 행이 없습니다."})
    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        execution = _dict(source.get("source_execution"))
        if execution.get("used_dummy_data") is True:
            notices.append({"type": "dummy_data", "message": "현재 결과는 더미 데이터 기준입니다."})
            break
    trace = _dict(payload.get("trace"))
    for item in _list(trace.get("warnings"))[:5]:
        if isinstance(item, dict):
            notices.append({"type": str(item.get("type") or "warning"), "message": str(item.get("message") or item)})
    for item in _list(trace.get("errors"))[:5]:
        if isinstance(item, dict):
            notices.append({"type": str(item.get("type") or "error"), "message": str(item.get("message") or item)})
    return notices


# 함수 설명: `_downloads()`는 저장된 data_ref에서 최종 답변에 제공할 다운로드 항목을 구성합니다.
def _downloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for ref in _list(payload.get("data_refs")):
        if isinstance(ref, dict):
            refs.append(deepcopy(ref))
    data_ref = _dict(_dict(payload.get("data")).get("data_ref"))
    if data_ref:
        refs.append(deepcopy(data_ref))
    return _dedupe_dicts(refs)


# 함수 설명: `_summary_basis()`는 답변 요약이 어떤 rows·지표·조건을 기준으로 작성됐는지 근거를 구성합니다.
def _summary_basis(applied_criteria: dict[str, Any]) -> list[str]:
    basis = []
    if applied_criteria.get("required_params"):
        basis.append("조회 필수 조건을 적용했습니다.")
    if applied_criteria.get("analysis_filters"):
        basis.append("공정/제품/상태 조건은 분석 단계에서 적용했습니다.")
    if applied_criteria.get("metrics"):
        basis.append("요청 지표를 기준으로 집계했습니다.")
    return basis


# 함수 설명: `_next_questions()`는 questions 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _next_questions(payload: dict[str, Any]) -> list[str]:
    data = _dict(payload.get("data"))
    row_count = _int(data.get("row_count"), len(_list(data.get("rows"))))
    if row_count <= 0:
        return ["조건을 넓혀서 다시 조회할까요?"]
    return ["이 결과를 제품별 또는 공정별로 더 나눠볼까요?", "원본 데이터를 내려받아 상세 Lot/Device를 확인할까요?"]


# 함수 설명: `_compact_source_results()`는 데이터 소스·결과에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_source_results(source_results: list[Any]) -> list[dict[str, Any]]:
    compact = []
    for source in source_results:
        if not isinstance(source, dict):
            continue
        compact.append(
            _omit_empty(
                {
                    "dataset_key": source.get("dataset_key"),
                    "source_alias": source.get("source_alias"),
                    "source_type": source.get("source_type"),
                    "status": source.get("status"),
                    "row_count": source.get("row_count"),
                    "applied_params": source.get("applied_params"),
                    "pandas_filters": source.get("pandas_filters") or source.get("applied_filters"),
                }
            )
        )
    return compact


# 함수 설명: `_build_next_turn_state()`는 다음 단계·TURN·상태 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def _build_next_turn_state(payload: dict[str, Any]) -> dict[str, Any]:
    # 이전 state 전체를 다시 복사하지 않고 현재 turn에서 확정된 정보만 새 상태로 만듭니다.
    # 이 방식으로 이전 retrieval job·source ref·download ref가 새 질문 뒤에도 남는 것을 차단합니다.
    previous_state = _dict(payload.get("state"))
    request = _dict(payload.get("request"))
    state: dict[str, Any] = {}
    session_id = str(request.get("session_id") or previous_state.get("session_id") or "").strip()
    if session_id:
        state["session_id"] = session_id
    state["last_question"] = _dict(payload.get("request")).get("question", "")
    state["last_answer_message"] = _clip_text(payload.get("answer_message"), 1000)
    state["current_data"] = _current_data_state(payload)
    followup_sources = _followup_source_results(payload)
    if followup_sources:
        state["followup_source_results"] = followup_sources
    runtime_source_refs = _runtime_source_refs(payload)
    if runtime_source_refs:
        state["runtime_source_refs"] = runtime_source_refs
    if request:
        # 하위 호환을 위한 전체 request 복사 대신 후속 판정에 필요한 현재 질문만 last_question에 유지합니다.
        state["last_question"] = request.get("question", state.get("last_question", ""))
    intent_plan = _compact_intent_plan(_dict(payload.get("intent_plan")))
    if intent_plan:
        state["last_intent_plan"] = intent_plan
    applied_criteria = _applied_criteria(payload)
    if applied_criteria:
        state["last_applied_criteria"] = applied_criteria
    return _omit_empty(state)


# 함수 설명: `_current_data_state()`는 현재 결과의 rows·columns·row_count·data_ref를 다음 질문용 작은 상태로 만듭니다.
def _current_data_state(payload: dict[str, Any]) -> dict[str, Any]:
    data = _dict(payload.get("data"))
    rows = _list(data.get("rows"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    return _omit_empty(
        {
            "row_count": _int(data.get("row_count"), len(rows)),
            "columns": columns,
            "result_columns": columns,
            "preview_rows": deepcopy(rows[:5]),
            "data_ref": deepcopy(data.get("data_ref")),
            "source_aliases": _source_aliases(payload),
            "source_dataset_keys": _source_dataset_keys(payload),
            "source_columns_by_alias": _source_columns_by_alias(payload),
        }
    )


# 함수 설명: `_followup_source_results()`는 후속 질문이 재사용할 source result를 preview와 참조 중심으로 압축합니다.
def _followup_source_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_sources = _dict(payload.get("runtime_sources"))
    result = []
    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        rows = _list(runtime_sources.get(alias))
        result.append(
            _omit_empty(
                {
                    "source_alias": alias,
                    "dataset_key": source.get("dataset_key"),
                    "source_type": source.get("source_type"),
                    "row_count": source.get("row_count") if source.get("row_count") is not None else len(rows),
                    "columns": _string_list(source.get("columns")) or _columns_from_rows(rows),
                    "preview_rows": deepcopy(rows[:5]),
                    "data_ref": deepcopy(source.get("data_ref")),
                    "applied_params": deepcopy(source.get("applied_params")),
                    "applied_filters": deepcopy(source.get("applied_filters") or source.get("pandas_filters")),
                }
            )
        )
    return [item for item in result if item]


# 함수 설명: `_runtime_source_refs()`는 메모리의 runtime source를 직접 저장하지 않고 재조회 가능한 source 참조만 구성합니다.
def _runtime_source_refs(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for ref in _list(payload.get("data_refs")):
        if not isinstance(ref, dict):
            continue
        if str(ref.get("role") or "") != "source_rows":
            continue
        alias = str(ref.get("source_alias") or "").strip()
        if alias:
            refs[alias] = deepcopy(ref)
    return refs


# 함수 설명: `_source_aliases()`는 aliases 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_aliases(payload: dict[str, Any]) -> list[str]:
    aliases = []
    for source in _list(payload.get("source_results")):
        if isinstance(source, dict):
            alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
            if alias and alias not in aliases:
                aliases.append(alias)
    for alias in _dict(payload.get("runtime_sources")):
        text = str(alias or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    return aliases


# 함수 설명: `_source_dataset_keys()`는 데이터셋·key 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_dataset_keys(payload: dict[str, Any]) -> list[str]:
    keys = []
    for source in _list(payload.get("source_results")):
        if isinstance(source, dict):
            key = str(source.get("dataset_key") or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


# 함수 설명: `_source_columns_by_alias()`는 컬럼·BY·alias 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_columns_by_alias(payload: dict[str, Any]) -> dict[str, list[str]]:
    runtime_sources = _dict(payload.get("runtime_sources"))
    result: dict[str, list[str]] = {}
    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        if not alias:
            continue
        columns = _string_list(source.get("columns")) or _columns_from_rows(_list(runtime_sources.get(alias)))
        if columns:
            result[alias] = columns
    for alias, rows in runtime_sources.items():
        text = str(alias or "").strip()
        if text and text not in result:
            columns = _columns_from_rows(_list(rows))
            if columns:
                result[text] = columns
    return result


# 함수 설명: `_compact_intent_plan()`는 의도 계획·PLAN에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_intent_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "analysis_kind": plan.get("analysis_kind"),
            "request_scope": plan.get("request_scope"),
            "reuse_strategy": plan.get("reuse_strategy"),
            "condition_resolution": deepcopy(_dict(plan.get("condition_resolution"))),
            "retrieval_jobs": _compact_retrieval_jobs(_list(plan.get("retrieval_jobs"))),
            "pandas_execution_plan": deepcopy(_list(plan.get("pandas_execution_plan"))[:8]),
            "pandas_function_cases": deepcopy(_list(plan.get("pandas_function_cases"))[:5]),
            "output_contract": deepcopy(_dict(plan.get("output_contract"))),
            "resolved_grain_plan": _compact_resolved_grain_plan(plan.get("resolved_grain_plan")),
        }
    )


# 함수 설명: 직전 결과 행 매칭에 필요한 grain identity만 다음 턴 상태에 작게 보존합니다.
def _compact_resolved_grain_plan(value: Any) -> dict[str, Any]:
    plan = _dict(value)
    return _omit_empty(
        {
            "metadata_ref": deepcopy(_dict(plan.get("metadata_ref"))),
            "source_alias": plan.get("source_alias"),
            "dataset_key": plan.get("dataset_key"),
            "canonical_columns": _string_list(plan.get("canonical_columns")),
            "grain_columns": _string_list(plan.get("grain_columns")),
            "column_mappings": [
                {
                    "canonical_key": item.get("canonical_key"),
                    "source_candidates": _string_list(item.get("source_candidates")),
                }
                for item in _list(plan.get("column_mappings"))[:20]
                if isinstance(item, dict)
            ],
            "strict": plan.get("strict"),
        }
    )


# 함수 설명: `_compact_retrieval_jobs()`는 데이터 조회·조회 작업에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_retrieval_jobs(jobs: list[Any]) -> list[dict[str, Any]]:
    compact = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        compact.append(
            _omit_empty(
                {
                    "dataset_key": job.get("dataset_key"),
                    "source_alias": job.get("source_alias"),
                    "source_type": job.get("source_type"),
                    "required_params": deepcopy(job.get("required_params")),
                    "filters": deepcopy(job.get("filters")),
                }
            )
        )
    return compact


# 함수 설명: `_group_by_columns()`는 의도 계획의 pandas 단계에서 실제 그룹 기준 컬럼을 추출합니다.
def _group_by_columns(pandas_plan: list[Any]) -> list[str]:
    columns: list[str] = []
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        for key in ("groupby_columns", "group_by", "group_by_columns", "group_columns"):
            value = step.get(key)
            if isinstance(value, list):
                for item in value:
                    text = str(item or "").strip()
                    if text and text not in columns:
                        columns.append(text)
            elif isinstance(value, str) and value.strip() and value.strip() not in columns:
                columns.append(value.strip())
    return columns


# 함수 설명: `_metric_columns()`는 결과 컬럼 중 수량·실적·비율처럼 답변 지표로 사용할 컬럼을 선별합니다.
def _metric_columns(payload: dict[str, Any]) -> list[str]:
    data = _dict(payload.get("data"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(_list(data.get("rows")))
    rows = _list(data.get("rows"))
    return [column for column in columns if _column_has_numeric_value(rows, column)]


# 함수 설명: `_column_has_numeric_value()`는 HAS·numeric·값 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _column_has_numeric_value(rows: list[Any], column: str) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None or isinstance(value, bool) or isinstance(value, str):
            continue
        try:
            number = float(value)
        except Exception:
            continue
        if number != number:
            continue
        return True
    return False


# 함수 설명: `_display_row()`는 행을 Markdown 또는 사용자 화면에서 안전하게 읽을 수 있는 표현으로 변환합니다.
def _display_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    if not columns:
        columns = [str(key) for key in row]
    return {column: _display_value(row.get(column, "")) for column in columns}


# 함수 설명: `_display_value()`는 None·숫자·복합 값을 사용자에게 읽기 좋은 짧은 문자열로 표시합니다.
def _display_value(value: Any) -> Any:
    formatted = _format_number(value)
    if formatted is not None:
        return formatted
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    return value


# 함수 설명: `_format_number()`는 number을 Markdown 또는 사용자 화면에서 안전하게 읽을 수 있는 표현으로 변환합니다.
def _format_number(value: Any) -> str | None:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number != number:
        return None
    if abs(number) >= 10000:
        k_value = number / 1000
        return f"{int(k_value):,}K" if float(k_value).is_integer() else f"{k_value:,.1f}K"
    return f"{int(number):,}" if float(number).is_integer() else f"{number:,.1f}"


# 함수 설명: `_columns_from_rows()`는 행 목록의 key 등장 순서를 유지하면서 결과 테이블의 컬럼 목록을 계산합니다.
def _columns_from_rows(rows: list[Any]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_int()`는 문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


# 함수 설명: BoolInput 또는 문자열 형태로 전달된 설정을 일관된 boolean 값으로 정규화합니다.
def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


# 함수 설명: `_clip_text()`는 문자열을 허용 길이 안으로 자르되 비어 있는 값과 말줄임 표시를 일관되게 처리합니다.
def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


# 함수 설명: `_omit_empty()`는 dict에서 빈 문자열·빈 목록·None 항목을 제거해 전달 payload를 작게 유지합니다.
def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


# 함수 설명: `_dedupe_dicts()`는 dicts의 중복을 제거하고 최초 등장 순서를 유지합니다.
def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


# 함수 설명: Complex 답변 모델에 전달할 결과·조건·근거만 최대 5행의 allowlist view로 구성합니다.
def build_answer_evidence_variables(payload_value: Any) -> dict[str, str]:
    payload = _payload(payload_value)
    plan = _dict(payload.get("intent_plan"))
    output_contract = _dict(plan.get("output_contract"))
    analysis = _dict(payload.get("analysis"))
    data = _dict(payload.get("data"))
    rows = _list(data.get("rows"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    evidence_columns = _answer_evidence_columns(columns, output_contract)
    pandas_execution = _dict(_dict(_dict(payload.get("trace")).get("inspection")).get("pandas_execution"))
    step_outputs = _list(analysis.get("step_outputs")) or _list(pandas_execution.get("step_outputs"))
    function_case_results = _list(analysis.get("function_case_results")) or _list(
        pandas_execution.get("function_case_results")
    )
    metric_columns = _metric_columns(payload)
    result_summary = _omit_empty(
        {
            "status": analysis.get("status"),
            "row_count": data.get("row_count", analysis.get("row_count", len(rows))),
            "columns": deepcopy(evidence_columns),
            "rows": _answer_evidence_rows(rows, output_contract, evidence_columns),
        }
    )
    applied_scope = _omit_empty(
        {
            "intent": _omit_empty({"analysis_kind": plan.get("analysis_kind")}),
            "criteria": _applied_criteria(payload),
            "retrieval": _compact_source_results(_list(payload.get("source_results"))),
            "pandas_execution": _omit_empty(
                {
                    "status": pandas_execution.get("status") or analysis.get("status"),
                    "execution_mode": analysis.get("execution_mode"),
                    "error": _compact_answer_error(pandas_execution.get("error") or analysis.get("error")),
                }
            ),
        }
    )
    answer_context = _omit_empty(
        {
            "number_display_policy": {
                "under_10000": "comma_full_number",
                "gte_10000": "k_unit",
                "display_only": True,
            },
            "result_interpretation_hints": _omit_empty(
                {
                    "is_empty_result": _int(data.get("row_count"), len(rows)) == 0 and not rows,
                    "has_zero_values": _answer_rows_have_zero(rows),
                    "primary_metric_columns": metric_columns,
                    "primary_dimension_columns": [column for column in evidence_columns if column not in set(metric_columns)],
                    "primary_metric": output_contract.get("primary_metric"),
                    "ordering": deepcopy(output_contract.get("ordering")),
                    "column_labels": deepcopy(_dict(output_contract.get("column_labels"))),
                    "operations": [
                        str(item.get("operation") or "").strip()
                        for item in _list(plan.get("pandas_execution_plan"))
                        if isinstance(item, dict) and str(item.get("operation") or "").strip()
                    ],
                    "result_segments": deepcopy(_list(output_contract.get("result_segments"))),
                    "segment_column": output_contract.get("segment_column"),
                    "rank_column": output_contract.get("rank_column"),
                }
            ),
            "step_outputs": _compact_answer_records(
                step_outputs,
                ("key", "description", "role", "row_count", "columns", "preview_rows"),
            ),
            "function_case_results": _compact_answer_records(
                function_case_results,
                ("function_name", "input_text", "description", "matched_count", "columns", "preview_rows"),
            ),
        }
    )
    trace = _dict(payload.get("trace"))
    diagnostics = {
        "warnings": _compact_answer_diagnostics(_list(trace.get("warnings"))),
        "errors": _compact_answer_diagnostics(_list(trace.get("errors"))),
    }
    return {
        "question": str(_dict(payload.get("request")).get("question") or ""),
        "result_summary_json": json.dumps(result_summary, ensure_ascii=False, separators=(",", ":"), default=str),
        "applied_scope_json": json.dumps(applied_scope, ensure_ascii=False, separators=(",", ":"), default=str),
        "answer_context_json": json.dumps(answer_context, ensure_ascii=False, separators=(",", ":"), default=str),
        "warnings_errors_json": json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":"), default=str),
    }


# 함수 설명: BoolInput이 켜진 Complex 경로에서만 AnswerEvidence를 직렬화하고 최종 prompt를 렌더합니다.
def build_lazy_llm_answer_prompt(
    payload_value: Any,
    prompt_template: Any,
    domain_answer_guidance: Any = "",
) -> str:
    template = _message_text(prompt_template).strip()
    if not template:
        return ""
    for previous, current in ANSWER_PROMPT_REFERENCE_REWRITES.items():
        template = template.replace(previous, current)
    variables = build_answer_evidence_variables(payload_value)
    variables["domain_answer_guidance"] = _message_text(domain_answer_guidance).strip()
    try:
        return template.format(**variables)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"answer prompt template rendering failed: {exc}") from exc


# 함수 설명: 구간형 결과는 각 구간 첫 행을 먼저 보존하고 모델용 결과 행은 최대 5개로 제한합니다.
def _answer_evidence_rows(
    rows: list[Any],
    output_contract: dict[str, Any],
    evidence_columns: list[str],
) -> list[dict[str, Any]]:
    candidates = [deepcopy(row) for row in rows if isinstance(row, dict)]
    if not candidates:
        return []
    selected: list[dict[str, Any]] = []
    selected_indexes: set[int] = set()
    segment_column = str(output_contract.get("segment_column") or "").strip()
    if segment_column:
        seen_segments: set[str] = set()
        for index, row in enumerate(candidates):
            segment = str(row.get(segment_column) or "")
            if segment in seen_segments:
                continue
            seen_segments.add(segment)
            selected.append(row)
            selected_indexes.add(index)
            if len(selected) >= ANSWER_EVIDENCE_ROW_LIMIT:
                return [_compact_answer_row(item, evidence_columns) for item in selected]
    for index, row in enumerate(candidates):
        if index in selected_indexes:
            continue
        selected.append(row)
        if len(selected) >= ANSWER_EVIDENCE_ROW_LIMIT:
            break
    return [_compact_answer_row(item, evidence_columns) for item in selected]


# 함수 설명: 계약의 segment/rank/grain/metric 순서를 우선해 모델 view 컬럼을 최대 16개로 제한합니다.
def _answer_evidence_columns(columns: list[str], output_contract: dict[str, Any]) -> list[str]:
    available = {str(column) for column in columns}
    preferred: list[str] = []

    # 함수 설명: `add()`는 여러 ADD 값을 순서와 중복 정책을 지키며 하나의 결과로 합칩니다.
    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text in available and text not in preferred:
            preferred.append(text)

    add(output_contract.get("segment_column"))
    add(output_contract.get("rank_column"))
    for key in ("grain_columns", "entity_grain_columns"):
        for column in _list(output_contract.get(key)):
            add(column)
    add(output_contract.get("primary_metric"))
    for key in ("metric_columns", "result_columns", "required_columns"):
        for column in _list(output_contract.get(key)):
            add(column)
    for column in columns:
        add(column)
    return preferred[:ANSWER_EVIDENCE_COLUMN_LIMIT]


# 함수 설명: 모델 view 행은 선택된 컬럼만 투영하고 장문 문자열·목록·객체 셀을 160자로 제한합니다.
def _compact_answer_row(row: dict[str, Any], evidence_columns: list[str]) -> dict[str, Any]:
    return {
        column: _compact_answer_cell(row.get(column))
        for column in evidence_columns
        if column in row
    }


# 함수 설명: 실행 결과 원본은 유지하면서 AnswerEvidence의 개별 셀만 안전한 길이로 축약합니다.
def _compact_answer_cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= ANSWER_EVIDENCE_CELL_LIMIT else value[: ANSWER_EVIDENCE_CELL_LIMIT - 1] + "…"
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= ANSWER_EVIDENCE_CELL_LIMIT else text[: ANSWER_EVIDENCE_CELL_LIMIT - 1] + "…"


# 함수 설명: 단계형 분석 근거는 식별·설명·건수·최대 2행 preview만 유지합니다.
def _compact_answer_records(items: list[Any], allowed_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in items[:ANSWER_EVIDENCE_ITEM_LIMIT]:
        if not isinstance(item, dict):
            continue
        record = {key: deepcopy(item.get(key)) for key in allowed_keys if item.get(key) not in (None, "", [], {})}
        if isinstance(record.get("preview_rows"), list):
            record["preview_rows"] = deepcopy(record["preview_rows"][:2])
        if isinstance(record.get("columns"), list):
            record["columns"] = [str(value) for value in record["columns"]]
        compact.append(record)
    return compact


# 함수 설명: 답변 생성에 필요한 오류 종류와 짧은 메시지만 보존합니다.
def _compact_answer_error(value: Any) -> dict[str, Any]:
    error = _dict(value)
    return _omit_empty(
        {
            "type": str(error.get("type") or ""),
            "message": str(error.get("message") or "")[:ANSWER_EVIDENCE_MESSAGE_LIMIT],
        }
    )


# 함수 설명: warning/error 목록을 제한하여 traceback과 중복 payload가 답변 모델로 전달되지 않게 합니다.
def _compact_answer_diagnostics(items: list[Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in items[:ANSWER_EVIDENCE_ITEM_LIMIT]:
        if isinstance(item, dict):
            compact.append(_compact_answer_error(item))
        elif item not in (None, ""):
            compact.append({"message": str(item)[:ANSWER_EVIDENCE_MESSAGE_LIMIT]})
    return [item for item in compact if item]


# 함수 설명: 결과 행에 숫자 0이 있는지만 계산하여 전체 행을 별도 context에 복제하지 않습니다.
def _answer_rows_have_zero(rows: list[Any]) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            if value is None or isinstance(value, bool):
                continue
            try:
                if not isinstance(value, str) and float(value) == 0:
                    return True
            except Exception:
                continue
    return False


# 함수 설명: `build_hybrid_answer_response()`는 hybrid·답변·응답 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def build_hybrid_answer_response(
    payload_value: Any,
    answer_prompt: Any = "",
    model_invoker: Any = None,
    use_llm_answer: Any = True,
    answer_prompt_template: Any = "",
    domain_answer_guidance: Any = "",
) -> dict[str, Any]:
    """Use deterministic answers for Fast and optionally for Complex."""

    started = perf_counter()
    payload = _payload(payload_value)
    contract = _dict(payload.get("simple_analysis_contract"))
    route = str(_dict(payload.get("analysis")).get("execution_route") or contract.get("route") or "complex").strip().lower()
    llm_answer_enabled = _bool(use_llm_answer, True)
    fast_trace = payload.setdefault("trace", {}).setdefault("inspection", {}).setdefault("fast_path", {})
    llm_calls = fast_trace.setdefault(
        "llm_calls",
        {"intent": 1, "pandas_generation": 0, "repair": 0, "answer": 0},
    )
    llm_calls["answer"] = 0
    continuation_runtime = _continuation_runtime(payload)
    pending_candidate = (
        str(continuation_runtime.get("status") or "").strip().lower() == "pending"
        and str(_dict(payload.get("analysis")).get("status") or "").strip().lower() in {"ok", "success"}
        and _int(_dict(payload.get("data")).get("row_count"), 0) > 0
    )
    continuation_unavailable = (
        _continuation_resume_unavailable_reason(payload) if pending_candidate else ""
    )
    pending_continuation = pending_candidate and not continuation_unavailable
    request = _dict(payload.get("request"))
    continuation_request = _dict(request.get("continuation"))
    force_skip = _bool(continuation_request.get("skip_intermediate_answer"), False)
    if continuation_unavailable:
        result = build_answer_response(
            payload,
            "1차 분석은 완료했지만 저장된 결과를 안전하게 다시 불러올 수 없어 후속 조회를 실행하지 않습니다.",
        )
        result.setdefault("analysis", {})["continuation_status"] = "followup_unavailable"
        result.setdefault("trace", {}).setdefault("warnings", []).append(
            {
                "type": continuation_unavailable,
                "message": "후속 조회에 필요한 세션·결과 저장 참조 계약이 완성되지 않았습니다.",
            }
        )
        result.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"].update(
            {
                "stage": "20_continuation_hybrid_answer_builder",
                "model_called": False,
                "policy": "followup_unavailable_without_model",
                "llm_answer_enabled": llm_answer_enabled,
                "continuation_status": "followup_unavailable",
                "reason": continuation_unavailable,
            }
        )
    elif pending_continuation:
        result = build_answer_response(payload, "다음 조회 단계를 준비했습니다.")
        result.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"].update(
            {
                "stage": "20_continuation_hybrid_answer_builder",
                "model_called": False,
                "policy": "pending_continuation_without_model",
                "llm_answer_enabled": llm_answer_enabled,
                "continuation_status": "pending",
            }
        )
    elif route == "fast":
        answer_text = _deterministic_fast_answer(payload, contract)
        result = build_answer_response(payload, answer_text)
        result.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"].update(
            {
                "stage": "20_hybrid_answer_builder",
                "model_called": False,
                "policy": "deterministic_fast_answer",
                "llm_answer_enabled": llm_answer_enabled,
            }
        )
    elif route == "blocked" or _execution_blocked(payload):
        result = build_answer_response(payload, "")
        result.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"].update(
            {
                "stage": "20_hybrid_answer_builder",
                "model_called": False,
                "policy": "blocked_without_model",
                "llm_answer_enabled": llm_answer_enabled,
            }
        )
    elif not llm_answer_enabled or force_skip:
        answer_text = (
            _authoritative_result_message(payload)
            or _deterministic_fast_answer(payload, contract)
        )
        result = build_answer_response(payload, answer_text)
        result.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"].update(
            {
                "stage": "20_hybrid_answer_builder",
                "model_called": False,
                "policy": "deterministic_complex_answer",
                "llm_answer_enabled": False if not llm_answer_enabled else True,
                "continuation_force_skip": force_skip,
            }
        )
    else:
        template = _message_text(answer_prompt_template).strip()
        prompt = (
            build_lazy_llm_answer_prompt(payload, template, domain_answer_guidance)
            if template
            else _answer_text(answer_prompt).strip()
        )
        response: Any = ""
        if prompt and model_invoker is not None:
            try:
                llm_calls["answer"] = int(llm_calls.get("answer") or 0) + 1
                response = model_invoker(prompt)
            except Exception as exc:
                payload.setdefault("trace", {}).setdefault("errors", []).append(
                    {"type": "answer_model_invocation_failed", "message": f"{type(exc).__name__}: {exc}"}
                )
        result = build_answer_response(payload, response)
        result.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"].update(
            {
                "stage": "20_hybrid_answer_builder",
                "model_called": bool(prompt and model_invoker is not None),
                "policy": "llm_complex_answer",
                "llm_answer_enabled": True,
                "prompt_chars": len(prompt),
                "answer_evidence_row_limit": ANSWER_EVIDENCE_ROW_LIMIT,
                "answer_evidence_column_limit": ANSWER_EVIDENCE_COLUMN_LIMIT,
                "answer_evidence_cell_limit": ANSWER_EVIDENCE_CELL_LIMIT,
            }
        )
    trace = result.setdefault("trace", {}).setdefault("inspection", {}).setdefault("fast_path", {})
    trace["llm_calls"] = deepcopy(llm_calls)
    trace.setdefault("timing_ms", {})["answer_build"] = round((perf_counter() - started) * 1000, 3)
    return result


# 함수 설명: 전체 intent plan에서 현재 dependent retrieval runtime 상태만 추출합니다.
def _continuation_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    plan = _dict(payload.get("intent_plan"))
    dependent = _dict(plan.get("dependent_retrieval_plan"))
    return _dict(dependent.get("runtime"))


# 함수 설명: 후속 단계를 재개할 세션·저장 결과 참조가 실제로 준비됐는지 검사합니다.
def _continuation_resume_unavailable_reason(payload: dict[str, Any]) -> str:
    request = _dict(payload.get("request"))
    if not str(request.get("session_id") or "").strip():
        return "continuation_session_missing"
    if not _continuation_result_ref(payload):
        return "continuation_result_ref_missing"
    inspection = _dict(_dict(payload.get("trace")).get("inspection"))
    result_store = _dict(inspection.get("result_store"))
    if str(result_store.get("status") or "").strip().lower() not in {
        "ok",
        "success",
        "complete",
        "completed",
    }:
        return "continuation_result_store_unavailable"
    return ""


# 함수 설명: 현재 payload의 분석 결과 저장 참조 ID를 문자열로 추출합니다.
def _continuation_result_ref(payload: dict[str, Any]) -> str:
    data = _dict(payload.get("data"))
    candidate = data.get("data_ref")
    if isinstance(candidate, dict):
        value = str(
            candidate.get("ref_id") or candidate.get("data_ref") or candidate.get("_id") or ""
        ).strip()
        if value:
            return value
    elif str(candidate or "").strip():
        return str(candidate).strip()
    for item in _list(payload.get("data_refs")):
        if isinstance(item, dict) and str(item.get("kind") or "") == "analysis_result":
            value = str(item.get("ref_id") or "").strip()
            if value:
                return value
    return ""


# 함수 설명: `_deterministic_fast_answer()`는 20 V2 Hybrid 답변 생성기 처리 중 FAST·답변 관련 값을 계산·변환하는 내부 helper입니다.
def _deterministic_fast_answer(payload: dict[str, Any], contract: dict[str, Any]) -> str:
    analysis = _dict(payload.get("analysis"))
    data = _dict(payload.get("data"))
    if str(analysis.get("status") or "").strip().lower() not in {"ok", "success"}:
        return ""
    row_count = _int(data.get("row_count"), 0)
    if row_count == 0:
        return "적용된 조건을 만족하는 데이터가 없습니다."
    recipe = str(contract.get("recipe") or "detail_query").strip().lower()
    output_contract = _dict(_dict(payload.get("intent_plan")).get("output_contract"))
    labels = _dict(output_contract.get("column_labels"))
    primary_metric = str(output_contract.get("primary_metric") or "").strip()
    if not primary_metric:
        primary_metric = next(
            (str(item.get("output_column") or "") for item in _list(contract.get("metrics")) if isinstance(item, dict)),
            "",
        )
    metric_label = str(labels.get(primary_metric) or primary_metric or "결과").strip()
    rows = [row for row in _list(data.get("rows")) if isinstance(row, dict)]
    first_value = rows[0].get(primary_metric) if rows and primary_metric in rows[0] else None
    formatted = _fixed_display_value(first_value)

    if recipe == "scalar_summary" and formatted:
        return f"{metric_label}은 {formatted}입니다."
    if recipe == "existence_summary":
        value = next(iter(rows[0].values()), False) if rows else False
        return "조건을 만족하는 데이터가 있습니다." if bool(value) else "조건을 만족하는 데이터가 없습니다."
    if recipe == "ranked_summary":
        direction = _first_order_direction(contract)
        word = "상위" if direction == "desc" else "하위"
        limit = _int(contract.get("limit"), row_count)
        return f"{metric_label} 기준 {word} {min(limit, row_count)}개 결과입니다."
    if recipe == "group_summary":
        return f"요청한 그룹 기준으로 {metric_label}을 집계한 결과는 {row_count}건입니다."
    if recipe == "frequency_summary":
        return f"값별 발생 빈도를 집계한 결과는 {row_count}건입니다."
    if recipe == "distinct_summary":
        return f"중복을 제거한 고유 결과는 {row_count}건입니다."
    if recipe == "list_summary":
        return f"그룹별 고유 항목 목록을 집계한 결과는 {row_count}건입니다."
    if recipe == "quality_summary":
        return f"지정한 데이터 품질 조건을 검사했습니다. {metric_label}은 {formatted or '결과 표'}입니다."
    if recipe == "latest_earliest":
        return "명시된 정렬 기준에 따른 최신 또는 최초 결과입니다."
    if recipe == "percent_of_total":
        return f"지정된 분모 범위에 따른 {metric_label} 구성비 결과는 {row_count}건입니다."
    if recipe == "rank_within_group":
        return f"지정된 그룹과 동률 정책에 따른 {metric_label} 순위 결과는 {row_count}건입니다."
    if recipe == "threshold_after_aggregate":
        return f"집계 후 임계값 조건을 통과한 결과는 {row_count}건입니다."
    if recipe == "time_bucket_summary":
        frequency = str(_dict(contract.get("calculation")).get("frequency") or "기간")
        return f"{frequency} 단위로 집계한 결과는 {row_count}건입니다."
    if recipe == "period_change":
        return f"지정된 기간 순서에 따른 {metric_label} 증감 결과는 {row_count}건입니다."
    if recipe == "running_total":
        return f"지정된 순서와 그룹 기준으로 계산한 {metric_label} 누적 결과는 {row_count}건입니다."
    if recipe == "moving_aggregate":
        window = _dict(contract.get("calculation")).get("window")
        return f"{window}개 구간 이동 계산 결과는 {row_count}건입니다."
    if recipe == "percentile_summary":
        percentile = _dict(contract.get("calculation")).get("percentile")
        return f"{metric_label}의 {percentile} 분위수 결과입니다."
    if recipe == "pivot_summary":
        return f"요청한 행·열 기준으로 재구성한 집계 결과는 {row_count}건입니다."
    return f"적용된 조건을 만족하는 결과는 총 {row_count}건입니다."


# 함수 설명: `_fixed_display_value()`는 20 V2 Hybrid 답변 생성기 처리 중 표시값·값 관련 값을 계산·변환하는 내부 helper입니다.
def _fixed_display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except Exception:
            return str(value)
        if number != number:
            return ""
        return f"{int(number):,}" if number.is_integer() else f"{number:,.4f}".rstrip("0").rstrip(".")
    return str(value)


# 함수 설명: `_first_order_direction()`는 20 V2 Hybrid 답변 생성기 처리 중 order·direction 관련 값을 계산·변환하는 내부 helper입니다.
def _first_order_direction(contract: dict[str, Any]) -> str:
    ordering = contract.get("ordering") if isinstance(contract.get("ordering"), list) else []
    first = ordering[0] if ordering and isinstance(ordering[0], dict) else {}
    return "asc" if str(first.get("direction") or "desc").strip().lower() == "asc" else "desc"


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class HybridAnswerBuilder(Component):
    display_name = "20 V2 Hybrid 답변 생성기"
    description = "Fast는 항상 고정 답변으로 만들고, Complex는 BoolInput 설정에 따라 답변 모델 또는 고정 답변을 사용합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        BoolInput(
            name="use_llm_answer",
            display_name="Complex 답변 LLM 사용",
            info="활성화하면 Complex만 답변 LLM을 호출하고, 비활성화하면 Fast와 Complex 모두 고정 로직으로 답변합니다.",
            value=True,
            required=False,
            advanced=False,
        ),
        MultilineInput(
            name="answer_prompt_template",
            display_name="답변 프롬프트 템플릿",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="domain_answer_guidance",
            display_name="도메인 특화 답변 지침",
            required=False,
            advanced=True,
        ),
        ModelInput(name="model", display_name="답변 언어 모델", required=False, real_time_refresh=True),
        SecretStrInput(name="api_key", display_name="답변 모델 API 키", required=False, advanced=True, real_time_refresh=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # 함수 설명: `update_build_config()`는 모델 선택에 따라 동적 입력 필드를 갱신하는 Langflow 빌드 lifecycle 함수입니다.
    def update_build_config(self, build_config: dict, field_value: str, field_name: str | None = None):
        from lfx.base.models.unified_models import (
            apply_provider_variable_config_to_build_config,
            get_language_model_options,
            get_provider_for_model_name,
            update_model_options_in_build_config,
        )

        build_config = update_model_options_in_build_config(
            component=self,
            build_config=build_config,
            cache_key_prefix="v2_answer_language_model_options",
            get_options_func=get_language_model_options,
            field_name=field_name,
            field_value=field_value,
        )
        current_model = field_value if field_name == "model" else build_config.get("model", {}).get("value")
        provider = ""
        if isinstance(current_model, list) and current_model:
            selected = current_model[0]
            provider = str(selected.get("provider") or "").strip()
            if not provider and selected.get("name"):
                provider = get_provider_for_model_name(str(selected["name"]))
        return apply_provider_variable_config_to_build_config(build_config, provider) if provider else build_config

    # 함수 설명: `_invoke_model()`는 model 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    def _invoke_model(self, prompt: str) -> Any:
        from lfx.base.models.unified_models import get_llm

        llm = get_llm(
            model=getattr(self, "model", None),
            user_id=getattr(self, "user_id", None),
            api_key=getattr(self, "api_key", None),
        )
        if llm is None or not hasattr(llm, "invoke"):
            raise RuntimeError("Answer Language Model이 연결되지 않았습니다.")
        return llm.invoke(prompt)

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=build_hybrid_answer_response(
                getattr(self, "payload", None),
                getattr(self, "answer_prompt", ""),
                self._invoke_model,
                getattr(self, "use_llm_answer", True),
                getattr(self, "answer_prompt_template", ""),
                getattr(self, "domain_answer_guidance", ""),
            )
        )
