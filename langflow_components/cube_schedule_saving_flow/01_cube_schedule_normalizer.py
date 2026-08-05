# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: CUBE 스케줄 추출 결과 정규화기
# 역할: 자연어에서 추출된 JSON을 cube.schedule.v1 source document로 결정론적으로 검증합니다.
# 주요 입력: 사용자 요청(user_request), LLM 추출 결과(llm_response)
# 주요 출력: 정규화 결과(schedule_payload)
# 유지보수 포인트: next_run_at과 실행상태는 source 문서에 저장하지 않습니다.
# =============================================================================

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.data import Data


SCHEDULE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}")


# 함수 설명: 사용자 요청과 LLM JSON을 source 전용 스케줄 문서로 정규화하고 모든 검증 오류를 함께 반환합니다.
def normalize_schedule(user_request: Any, llm_response: Any) -> dict[str, Any]:
    request_text = _text(user_request)
    extracted, parse_error = _json_object(_text(llm_response))
    errors: list[dict[str, str]] = []
    if parse_error:
        errors.append({"type": "invalid_llm_json", "message": parse_error})
    employee_id = str(extracted.get("employee_id") or "").strip()
    channel_id = str(extracted.get("channel_id") or "").strip()
    question = str(extracted.get("question") or "").strip()
    enabled = _bool(extracted.get("enabled"), True)
    schedule = extracted.get("schedule") if isinstance(extracted.get("schedule"), dict) else {}
    schedule_type = str(schedule.get("type") or "").strip().lower()
    timezone_name = str(schedule.get("timezone") or "Asia/Seoul").strip() or "Asia/Seoul"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        errors.append({"type": "invalid_timezone", "message": f"알 수 없는 timezone입니다: {timezone_name}"})
    normalized_schedule: dict[str, Any] = {"type": schedule_type, "timezone": timezone_name}
    if schedule_type == "interval":
        minutes = _int(schedule.get("minutes"))
        if minutes < 5:
            errors.append({"type": "invalid_interval", "message": "interval minutes는 5 이상이어야 합니다."})
        else:
            normalized_schedule["minutes"] = minutes
    elif schedule_type == "cron":
        expression = str(schedule.get("expression") or "").strip()
        cron_error = _cron_error(expression)
        if cron_error:
            errors.append({"type": "invalid_cron", "message": cron_error})
        else:
            normalized_schedule["expression"] = expression
    else:
        errors.append({"type": "invalid_schedule_type", "message": "schedule.type은 interval 또는 cron이어야 합니다."})
    if not employee_id:
        errors.append({"type": "missing_employee_id", "message": "작업자 사번(employee_id)이 필요합니다."})
    if not question:
        errors.append({"type": "missing_question", "message": "CUBE 챗봇에 전달할 질의(question)가 필요합니다."})
    if len(question) > 8000:
        errors.append({"type": "question_too_long", "message": "question은 8000자를 초과할 수 없습니다."})
    schedule_id = str(extracted.get("schedule_id") or "").strip()
    if not schedule_id and employee_id and question and schedule_type:
        seed = json.dumps(
            {"employee_id": employee_id, "question": question, "schedule": normalized_schedule},
            ensure_ascii=False,
            sort_keys=True,
        )
        schedule_id = f"schedule:{employee_id}:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"
    if schedule_id and not SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
        errors.append({"type": "invalid_schedule_id", "message": "schedule_id 형식이 올바르지 않습니다."})
    document = {
        "schema_version": "cube.schedule.v1",
        "schedule_id": schedule_id,
        "employee_id": employee_id,
        "channel_id": channel_id,
        "question": question,
        "schedule": normalized_schedule,
        "enabled": enabled,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "response_type": "cube_schedule_authoring",
        "status": "ready_to_save" if not errors else "error",
        "success": not errors,
        "ready_to_save": not errors,
        "schedule_document": document,
        "source_request": request_text[:2000],
        "errors": errors,
        "warnings": [] if channel_id else [{"type": "missing_channel_id", "message": "channel_id가 없어 실행 서버의 사번-채널 매핑을 사용합니다."}],
    }


# 함수 설명: Markdown code fence가 포함될 수 있는 LLM 문자열에서 최상위 JSON object 하나를 안전하게 추출합니다.
def _json_object(text: str) -> tuple[dict[str, Any], str]:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return {}, "LLM 응답에서 JSON object를 찾지 못했습니다."
    try:
        value = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        return {}, f"JSON 해석 실패: {exc}"
    return (deepcopy(value), "") if isinstance(value, dict) else ({}, "최상위 JSON은 object여야 합니다.")


# 함수 설명: 5-field cron의 문법과 각 field 범위를 검사하고 첫 번째 오류 메시지를 반환합니다.
def _cron_error(expression: str) -> str:
    fields = str(expression or "").split()
    if len(fields) != 5:
        return "cron expression은 분 시 일 월 요일의 5개 field여야 합니다."
    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    for field, (minimum, maximum) in zip(fields, limits, strict=True):
        for part in field.split(","):
            match = re.fullmatch(r"(\*|\d+|\d+-\d+)(?:/(\d+))?", part)
            if not match:
                return f"지원하지 않는 cron field입니다: {field}"
            base, step = match.groups()
            if step and int(step) <= 0:
                return "cron step은 1 이상이어야 합니다."
            numbers = [int(value) for value in re.findall(r"\d+", base)]
            if any(value < minimum or value > maximum for value in numbers):
                return f"cron 값이 허용 범위를 벗어났습니다: {field}"
            if len(numbers) == 2 and numbers[0] > numbers[1]:
                return f"cron 범위의 시작이 끝보다 큽니다: {field}"
    return ""


# 함수 설명: Langflow Message 또는 일반 값을 앞뒤 공백이 제거된 문자열로 변환합니다.
def _text(value: Any) -> str:
    return str(getattr(value, "text", value) or "").strip()


# 함수 설명: interval 입력을 예외 없이 정수로 변환하고 실패하면 검증 실패용 0을 반환합니다.
def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# 함수 설명: 사용자 또는 LLM의 여러 boolean 표현을 정규화하고 알 수 없는 값에는 기본값을 적용합니다.
def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on", "active", "enabled"}:
        return True
    if text in {"false", "0", "no", "off", "inactive", "disabled"}:
        return False
    return default


# Langflow 컴포넌트 클래스: CUBE 스케줄 후보를 검증된 cube.schedule.v1 Data로 제공하는 standalone 노드입니다.
class CubeScheduleNormalizer(Component):
    display_name = "01 CUBE 스케줄 정규화기"
    description = "자연어 추출 JSON을 cube.schedule.v1 source 문서로 검증합니다."
    inputs = [
        MessageTextInput(name="user_request", display_name="사용자 요청", required=True),
        MessageTextInput(name="llm_response", display_name="LLM 추출 결과", required=True),
    ]
    outputs = [Output(name="schedule_payload", display_name="스케줄 페이로드", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 연결된 사용자 요청과 LLM 결과를 한 번 정규화해 다음 Writer가 받는 Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_schedule(getattr(self, "user_request", ""), getattr(self, "llm_response", "")))
