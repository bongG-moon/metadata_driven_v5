from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .config import CubeSchedulerConfig
from .models import ScheduledQuery


@dataclass(frozen=True)
class CubeDeliveryResult:
    success: bool
    retryable: bool = False
    status_code: int = 0
    message: str = ""
    response: dict[str, Any] | None = None


def render_scheduled_query(query: ScheduledQuery, config: CubeSchedulerConfig) -> dict[str, Any]:
    content: dict[str, Any] = {
        "header": {},
        "body": {
            "bodystyle": "none",
            "row": [
                {
                    "bgcolor": "#ffffff",
                    "border": False,
                    "align": "left",
                    "width": "100%",
                    "column": [
                        {
                            "type": "label",
                            "control": {
                                "active": True,
                                "text": [query.question],
                                "color": "#000000",
                            },
                        }
                    ],
                }
            ],
        },
        "metadata": {
            "kind": query.kind,
            "schedule_id": query.schedule_id,
            "run_id": query.run_id,
            "dedupe_key": query.dedupe_key,
        },
    }
    if config.cube_callback_address:
        content["process"] = {
            "callbacktype": "url",
            "callbackaddress": config.cube_callback_address,
            "requestid": ["cubeuniquename", "cubechannelid"],
        }
    return {
        "richnotification": {
            "header": {
                "from": config.cube_bot_id,
                "token": config.cube_bot_token,
                "to": {
                    "uniquename": [query.employee_id],
                    "channelid": [query.channel_id],
                },
            },
            "content": [content],
        }
    }


class HttpCubeTransport:
    def __init__(
        self,
        config: CubeSchedulerConfig,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()

    def send(self, query: ScheduledQuery) -> CubeDeliveryResult:
        errors = self.config.validate()
        if errors:
            return CubeDeliveryResult(False, False, message="; ".join(errors))
        payload = render_scheduled_query(query, self.config)
        try:
            response = self.session.post(
                self.config.cube_outbound_url,
                json=payload,
                timeout=(
                    self.config.connect_timeout_seconds,
                    self.config.read_timeout_seconds,
                ),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            return CubeDeliveryResult(False, True, message=f"{type(exc).__name__}: {exc}")
        parsed: Any = None
        if response.content:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
        parsed_object = parsed if isinstance(parsed, dict) else None
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            return CubeDeliveryResult(
                False,
                True,
                response.status_code,
                "Retryable Cube HTTP response.",
                parsed_object,
            )
        if not response.ok:
            return CubeDeliveryResult(
                False,
                False,
                response.status_code,
                "Cube rejected the scheduled query.",
                parsed_object,
            )
        if parsed_object:
            status_value = str(parsed_object.get("status") or parsed_object.get("result") or "success").strip().lower()
            if status_value not in self.config.accepted_statuses:
                return CubeDeliveryResult(
                    False,
                    False,
                    response.status_code,
                    f"Unexpected Cube status: {status_value}",
                    parsed_object,
                )
        return CubeDeliveryResult(True, False, response.status_code, "accepted", parsed_object)
