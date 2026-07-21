# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: GaiA Output Adapter
# 역할: 최종 답변과 참고자료를 GaiA 응답 계약으로 정규화해 표준 Chat Output으로 전달합니다.
# 주요 입력: 답변 Message/Data/DataFrame, 문서·URL·이미지·노하우·후속 질문 참조
# 주요 출력: Langflow Message, 구조화된 GaiA Response Data
# 유지보수 포인트: 어댑터는 메시지를 저장하지 않고 표준 Chat Output이 한 번만 저장합니다.
# =============================================================================

import json
import logging
from typing import Any

import orjson
from fastapi.encoders import jsonable_encoder

from lfx.base.io.chat import ChatComponent
from lfx.helpers.data import safe_convert
from lfx.inputs.inputs import HandleInput
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message
from lfx.template.field.base import Output
from lfx.utils.constants import MESSAGE_SENDER_AI

logger = logging.getLogger(__name__)

REFERENCE_TYPES = {
    "docs": "doc",
    "images": "image",
    "knowhows": "knowhow",
    "followup_questions": "followup_question",
    "urls": "url",
}
REFERENCE_KEYS = tuple(REFERENCE_TYPES.keys())
OUTPUT_METADATA_KEYS = (*REFERENCE_KEYS, "trace_id", "usage")

REFERENCE_ALLOWED_FIELDS = {
    "docs": {
        "type",
        "srcType",
        "name",
        "docId",
        "extension",
        "path",
        "url",
        "page",
        "totalPage",
        "startPage",
        "chunkId",
        "score",
        "quote",
        "sectionTitle",
    },
    "urls": {"type", "id", "name", "title", "url", "snippet", "publishedAt", "source", "rank"},
    "images": {"type", "id", "value"},
    "knowhows": {
        "type",
        "id",
        "knowhow_id",
        "knowhow_no",
        "knowhow",
        "tab_name",
        "tab_id",
        "user_id",
        "user_name",
        "user_department",
    },
    "followup_questions": {"type", "id", "value"},
}
REFERENCE_REQUIRED_FIELDS = {
    "docs": ("srcType", "name", "docId", "startPage"),
    "urls": (),
    "images": ("id", "value"),
    "knowhows": ("id", "knowhow_id", "knowhow"),
    "followup_questions": ("value",),
}
REFERENCE_NUMBER_FIELDS = {
    "docs": {"page", "totalPage", "startPage"},
    "urls": {"rank"},
    "images": set(),
    "knowhows": {"knowhow_no"},
    "followup_questions": set(),
}


# Langflow 컴포넌트 클래스: 최종 답변과 참고자료를 GaiA answer/metadata에 담아 표준 Chat Output으로 전달하는 어댑터입니다.
class GaiAOutputAdapter(ChatComponent):
    display_name = "GaiA Output Adapter"
    description = (
        "최종 답변과 참고자료를 GaiA AgentBuilder가 이해하는 answer/metadata 형식으로 정리합니다. "
        "변환된 Message는 뒤의 표준 Chat Output이 Playground와 실행 API에 출력합니다."
    )
    documentation: str = "https://docs.langflow.org/chat-input-and-output"
    icon = "MessagesSquare"
    name = "GaiAOutputAdapter"
    minimized = True

    inputs = [
        HandleInput(
            name="input_value",
            display_name="answer",
            input_types=["Data", "DataFrame", "Message"],
            required=True,
        ),
        HandleInput(name="docs", display_name="docs", input_types=["Data", "DataFrame"], required=False),
        HandleInput(name="urls", display_name="urls", input_types=["Data", "DataFrame"], required=False),
        HandleInput(name="images", display_name="images", input_types=["Data", "DataFrame"], required=False),
        HandleInput(
            name="knowhows",
            display_name="knowhows",
            input_types=["Data", "DataFrame"],
            required=False,
        ),
        HandleInput(
            name="followup_questions",
            display_name="followup_questions",
            input_types=["Data", "DataFrame"],
            required=False,
        ),
    ]

    outputs = [
        Output(
            display_name="message",
            name="message",
            method="message_response",
            types=["Message"],
            group_outputs=True,
        ),
        Output(
            display_name="GaiA Response",
            name="gaia_response",
            method="gaia_response_output",
            types=["Data"],
            group_outputs=True,
        ),
    ]

    # 함수 설명: None, NaN, 빈 문자열과 빈 컬렉션을 공통 빈 값으로 판정합니다.
    def _is_empty(self, value: Any) -> bool:
        if value is None:
            return True
        try:
            if value != value:
                return True
        except Exception:
            pass
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple, set, dict)):
            return len(value) == 0
        return False

    # 함수 설명: Langflow Data·DataFrame·Message와 JSON 문자열을 일반 Python 값으로 변환합니다.
    def _plain_value(self, value: Any) -> Any:
        if self._is_empty(value):
            return {}
        if isinstance(value, Data):
            return jsonable_encoder(value.data)
        if isinstance(value, DataFrame):
            try:
                return [jsonable_encoder(row.to_dict()) for _, row in value.iterrows()]
            except Exception:
                return jsonable_encoder(value)
        if isinstance(value, Message):
            data = getattr(value, "data", None) or getattr(value, "a2a_data", None)
            if not self._is_empty(data):
                return jsonable_encoder(data)
            text = getattr(value, "text", "")
            return {"text": text} if text else {}
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return {}
            try:
                return jsonable_encoder(orjson.loads(stripped))
            except Exception:
                return stripped
        return jsonable_encoder(value)

    # 함수 설명: 입력 값을 구조화된 dict로 변환하며 그 외 형식은 빈 dict로 처리합니다.
    def _as_dict(self, value: Any) -> dict:
        plain_value = self._plain_value(value)
        return plain_value if isinstance(plain_value, dict) else {}

    # 함수 설명: 최종 답변 본문을 Message/Data/일반 값에서 일관된 문자열로 꺼냅니다.
    def _text_value(self, value: Any) -> str:
        if isinstance(value, Message):
            return str(getattr(value, "text", "") or "")
        if isinstance(value, Data):
            data = getattr(value, "data", None)
            if isinstance(data, dict) and data.get("text") not in (None, ""):
                return str(data.get("text"))
        return safe_convert(value)

    # 함수 설명: DataFrame의 열 중심 dict를 행 목록으로 바꿀 때 숫자 인덱스를 우선 정렬합니다.
    def _index_sort_key(self, key: Any):
        text = str(key)
        return (0, int(text)) if text.isdigit() else (1, text)

    # 함수 설명: 동일 길이의 열 중심 dict를 GaiA 참조자료 행 목록으로 변환합니다.
    def _records_from_column_dict(self, value: Any):
        if not isinstance(value, dict) or not value:
            return None
        columns = {}
        lengths = set()
        for key, column in value.items():
            if isinstance(column, dict):
                ordered = [column[item] for item in sorted(column.keys(), key=self._index_sort_key)]
            elif isinstance(column, list):
                ordered = column
            else:
                return None
            columns[str(key)] = ordered
            lengths.add(len(ordered))
        if len(lengths) != 1:
            return None
        count = lengths.pop()
        return [{key: columns[key][index] for key in columns} for index in range(count)]

    # 함수 설명: docs={docs:[...]}처럼 한 번 더 감싼 참조자료 값을 풀어냅니다.
    def _unwrap_reference_collection(self, field_name: str, value: Any) -> Any:
        if isinstance(value, dict) and field_name in value:
            return value.get(field_name)
        return value

    # 함수 설명: 필수 문자열 필드를 공백 제거한 값으로 정규화합니다.
    def _string_value(self, value: Any) -> str:
        if self._is_empty(value):
            return ""
        return str(value).strip()

    # 함수 설명: 페이지와 순번 필드를 정수로 변환하며 변환 불가 값은 None으로 반환합니다.
    def _integer_value(self, value: Any) -> int | None:
        if self._is_empty(value):
            return None
        try:
            return int(float(str(value).strip()))
        except Exception:
            return None

    # 함수 설명: 검색 실패를 나타내는 placeholder 참조자료는 최종 응답에서 제외합니다.
    def _is_placeholder_reference(self, item: Any) -> bool:
        return isinstance(item, dict) and str(item.get("status") or "").strip().lower() in {
            "not_found",
            "no_result",
            "empty",
        }

    # 함수 설명: 참조자료의 숫자와 문자열 필드를 GaiA 계약 타입으로 맞춥니다.
    def _coerce_reference_value(self, field_name: str, key: str, value: Any) -> Any:
        if key in REFERENCE_NUMBER_FIELDS[field_name]:
            number = self._integer_value(value)
            return number if number is not None else value
        if isinstance(value, str):
            return value.strip()
        return value

    # 함수 설명: 참조자료 한 건의 허용 필드, type, 필수 값을 검증하고 정규화합니다.
    def _validate_reference_item(self, field_name: str, item: dict) -> dict:
        allowed = REFERENCE_ALLOWED_FIELDS[field_name]
        canonical_type = REFERENCE_TYPES[field_name]
        unknown_keys = sorted(
            str(key) for key in item if str(key) not in allowed and not self._is_empty(item.get(key))
        )
        if unknown_keys:
            raise ValueError(f"{field_name} reference item has unsupported field(s): {', '.join(unknown_keys)}")
        item_type = item.get("type")
        if not self._is_empty(item_type) and self._string_value(item_type) != canonical_type:
            raise ValueError(f"{field_name} reference item requires type={canonical_type!r}")
        normalized = {}
        for key in allowed:
            if key in item and not self._is_empty(item.get(key)):
                normalized[key] = self._coerce_reference_value(field_name, key, item.get(key))
        for required_key in REFERENCE_REQUIRED_FIELDS[field_name]:
            if self._is_empty(normalized.get(required_key)):
                raise ValueError(f"{field_name} reference item requires a non-empty {required_key}")
        if field_name == "docs" and self._string_value(normalized.get("srcType")) != "upload":
            raise ValueError('docs reference item requires srcType="upload"')
        if field_name == "knowhows" and self._string_value(normalized.get("id")) != self._string_value(
            normalized.get("knowhow_id")
        ):
            raise ValueError("knowhows reference item requires id and knowhow_id to match")
        return normalized

    # 함수 설명: 참조자료 한 건을 일반 dict로 변환한 뒤 필드 계약을 검증합니다.
    def _normalize_reference_item(self, field_name: str, item: Any) -> dict:
        plain_item = self._plain_value(item)
        if not isinstance(plain_item, dict):
            raise ValueError(f"{field_name} reference item must be an object")
        return self._validate_reference_item(field_name, plain_item)

    # 함수 설명: 여러 입력 표현을 GaiA 참조자료 객체 목록으로 통일합니다.
    def _normalize_reference_collection(self, field_name: str, value: Any) -> list[dict]:
        plain_value = self._unwrap_reference_collection(field_name, self._plain_value(value))
        if self._is_empty(plain_value):
            return []
        records = self._records_from_column_dict(plain_value)
        if records is not None:
            items = records
        elif isinstance(plain_value, (list, tuple)):
            items = plain_value
        else:
            items = [plain_value]
        normalized = []
        for item in items:
            if self._is_empty(item) or self._is_placeholder_reference(item):
                continue
            normalized.append(self._normalize_reference_item(field_name, item))
        return normalized

    # 함수 설명: 개별 입력 포트의 참조자료 중 실제 값이 있는 항목만 응답 payload에 구성합니다.
    def _reference_payload_from_ports(self) -> dict:
        payload: dict = {}
        for field_name in REFERENCE_KEYS:
            references = self._normalize_reference_collection(field_name, getattr(self, field_name, None))
            if references:
                payload[field_name] = references
        return payload

    # 함수 설명: 앞 단계 Message.data의 nested metadata 또는 top-level canonical key에서 참조자료를 읽습니다.
    def _reference_payload_from_answer_data(self, answer_data: dict[str, Any]) -> dict:
        metadata = answer_data.get("metadata") if isinstance(answer_data.get("metadata"), dict) else {}
        payload: dict[str, list[dict[str, Any]]] = {}
        for field_name in REFERENCE_KEYS:
            value = metadata.get(field_name) if field_name in metadata else answer_data.get(field_name)
            references = self._normalize_reference_collection(field_name, value)
            if references:
                payload[field_name] = references
        return payload

    # 함수 설명: Message와 개별 포트에서 들어온 참조자료를 순서대로 합치고 동일 항목을 한 번만 유지합니다.
    def _merge_reference_payloads(self, *payloads: dict[str, Any]) -> dict:
        merged: dict[str, list[dict[str, Any]]] = {}
        for field_name in REFERENCE_KEYS:
            items: list[dict[str, Any]] = []
            seen: set[str] = set()
            for payload in payloads:
                for item in payload.get(field_name, []) if isinstance(payload, dict) else []:
                    marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    items.append(item)
            if items:
                merged[field_name] = items
        return merged

    # 함수 설명: 토큰 사용량 값을 오류 없이 정수로 변환합니다.
    def _int_token(self, value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    # 함수 설명: 모델 사용량 한 건을 prompt/completion/total 토큰 계약으로 정규화합니다.
    def _normalize_usage_item(self, value: Any) -> dict:
        safe_value = self._plain_value(value)
        if not isinstance(safe_value, dict):
            return {}
        prompt_tokens = self._int_token(safe_value.get("prompt_tokens"))
        completion_tokens = self._int_token(safe_value.get("completion_tokens"))
        total_tokens = self._int_token(safe_value.get("total_tokens")) or prompt_tokens + completion_tokens
        if not (prompt_tokens or completion_tokens or total_tokens):
            return {}
        normalized = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        if safe_value.get("model"):
            normalized["model"] = str(safe_value.get("model"))
        return normalized

    # 함수 설명: 중첩된 usage 입력을 펼치고 중복 항목을 제거합니다.
    def _normalize_usage_list(self, *values: Any) -> list[dict]:
        usage_items = []
        seen = set()
        stack = list(values)
        while stack:
            value = self._plain_value(stack.pop(0))
            if self._is_empty(value):
                continue
            if isinstance(value, list):
                stack[0:0] = value
                continue
            item = self._normalize_usage_item(value)
            if item:
                marker = str(sorted(item.items()))
                if marker not in seen:
                    seen.add(marker)
                    usage_items.append(item)
        return usage_items

    # 함수 설명: 누락된 참조자료도 빈 배열로 포함하는 고정 GaiA metadata envelope를 만듭니다.
    def _response_metadata(self, port_references: dict, usage_items: list[dict], trace_id: Any = "") -> dict:
        return {
            "docs": port_references.get("docs", []),
            "images": port_references.get("images", []),
            "knowhows": port_references.get("knowhows", []),
            "followup_questions": port_references.get("followup_questions", []),
            "urls": port_references.get("urls", []),
            "trace_id": str(trace_id or ""),
            "usage": usage_items,
        }

    # 함수 설명: 상태 payload를 한글이 보존되는 JSON 로그 문자열로 안전하게 변환합니다.
    def _json_for_log(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    # 함수 설명: 답변 본문과 참조자료 및 사용량을 최종 GaiA 응답 계약으로 조립합니다.
    def _build_response_payload(self) -> dict:
        input_value = getattr(self, "input_value", "")
        text = self._text_value(input_value)
        answer_data = self._as_dict(input_value)
        answer_metadata = answer_data.get("metadata") if isinstance(answer_data.get("metadata"), dict) else {}
        port_references = self._merge_reference_payloads(
            self._reference_payload_from_answer_data(answer_data),
            self._reference_payload_from_ports(),
        )
        output_metadata = self._response_metadata(
            port_references,
            self._normalize_usage_list(answer_metadata.get("usage"), answer_data.get("usage")),
            answer_metadata.get("trace_id") or answer_data.get("trace_id"),
        )
        return {"answer": text, "metadata": output_metadata}

    # 함수 설명: 운영 로그에는 원문 대신 답변 길이와 참조자료 개수 등 안전한 요약만 남깁니다.
    def _status_from_payload(self, payload: dict) -> dict:
        metadata_payload = payload.get("metadata") or {}
        return {
            "component": "GaiA Output Adapter",
            "answer_length": len(str(payload.get("answer") or "")),
            "metadata_keys": sorted(str(key) for key in metadata_payload.keys()),
            "reference_counts": {
                str(key): len(value)
                for key, value in metadata_payload.items()
                if key in REFERENCE_KEYS and isinstance(value, list)
            },
            "usage_count": len(metadata_payload.get("usage") or []),
        }

    # 함수 설명: 최종 응답의 안전한 상태 요약을 서버 로그와 컴포넌트 상태에 기록합니다.
    def _log_payload(self, payload: dict) -> None:
        debug_text = self._json_for_log(self._status_from_payload(payload))
        logger.warning("GAIA_DEBUG GaiA Output payload=%s", debug_text)
        print(f"GAIA_DEBUG GaiA Output payload={debug_text}", flush=True)

    # 주요 메서드: GaiA payload를 Message에 담되 저장은 뒤의 표준 Chat Output에 위임합니다.
    async def message_response(self) -> Message:
        payload = self._build_response_payload()
        input_value = getattr(self, "input_value", None)
        message = input_value if isinstance(input_value, Message) else Message(text=payload["answer"])
        message.text = payload["answer"]
        message.sender = MESSAGE_SENDER_AI
        message.sender_name = "AI"
        if not getattr(message, "session_id", ""):
            message.session_id = getattr(getattr(self, "graph", None), "session_id", "") or ""
        if not isinstance(getattr(message, "data", None), dict):
            message.data = {}
        message.data["gaia_response"] = payload
        message.metadata = payload["metadata"]
        self.status = message
        return message

    # 주요 메서드: HTTP/Tool 호출이 중간 Message 저장 없이 사용할 구조화된 GaiA Response Data를 반환합니다.
    async def gaia_response_output(self) -> Data:
        payload = self._build_response_payload()
        self._log_payload(payload)
        result = Data(data=payload)
        self.status = result
        return result
