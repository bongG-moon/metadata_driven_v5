"""사번별 H-API 조회 및 사내 SMTP 릴레이 발송. Outlook 설치 불필요."""
from __future__ import annotations

import asyncio
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from mail_renderer import render_mail

SENDER = "ptmorepkg.bot@sk.com"


class MailDeliveryError(RuntimeError):
    pass


def valid_address(value):
    address = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", address):
        raise MailDeliveryError("invalid_employee_email")
    return address


async def lookup_employees(client, employee_ids):
    ids = list(dict.fromkeys(str(value).strip() for value in employee_ids))
    if not all(re.fullmatch(r"\d{7}", value) for value in ids):
        raise MailDeliveryError("invalid_employee_id")
    token = os.getenv("PTMORE_EMPLOYEE_HAPI_TOKEN", "").strip()
    endpoint = os.getenv("PTMORE_EMPLOYEE_HAPI_URL", "").strip()
    if not token or not endpoint:
        raise MailDeliveryError("employee_hapi_not_configured")
    try:
        response = await client.post(endpoint, headers={"h-api-token": token, "Content-Type": "application/json"},
                                     json={"bindParams": [",".join(ids)]}, timeout=10.0)
        response.raise_for_status()
        rows = response.json()
    except Exception:
        raise MailDeliveryError("employee_hapi_failed") from None
    if not isinstance(rows, list):
        raise MailDeliveryError("employee_hapi_invalid_response")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        employee = str(row.get("EMPNO", "")).strip()
        if employee not in ids:
            continue
        record = {"email": valid_address(row.get("EMAIL")),
                  "name": str(row.get(os.getenv("PTMORE_EMPLOYEE_HAPI_NAME_FIELD", "EMP_NM")) or "").strip()}
        if employee in result and result[employee] != record:
            raise MailDeliveryError("ambiguous_employee_mapping")
        result[employee] = record
    if any(employee not in result for employee in ids):
        raise MailDeliveryError("employee_email_not_found")
    return result


def build_message(recipient, registrant, question, answer):
    to = valid_address(recipient["email"])
    cc = valid_address(registrant["email"])
    message = MIMEMultipart("alternative")
    message["From"] = SENDER
    message["To"] = formataddr((recipient.get("name", ""), to))
    message["Cc"] = formataddr((registrant.get("name", ""), cc))
    message["Subject"] = "[PTMORE PKG Agent] 스케줄링 실행 결과"
    text = f"안녕하세요! PTMORE PKG Agent 스케줄링 실행 결과입니다 😀.\n실행 질문: {question}\n\n{answer}"
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(render_mail(question, answer), "html", "utf-8"))
    return message, list(dict.fromkeys([to, cc]))


def smtp_send(message, recipients):
    try:
        with smtplib.SMTP(os.getenv("PTMORE_SMTP_HOST", "exhub.skhynix.com"),
                          int(os.getenv("PTMORE_SMTP_PORT", "25")), timeout=10) as server:
            refused = server.sendmail(SENDER, recipients, message.as_string())
            if refused:
                raise MailDeliveryError("smtp_recipient_refused")
    except MailDeliveryError:
        raise
    except Exception:
        # SMTP 응답에는 주소/서버 정보가 포함될 수 있으므로 원문을 기록하지 않습니다.
        raise MailDeliveryError("smtp_failed_or_uncertain") from None


async def send_employee_mail(client, employee_id, registrant_id, question, answer, *, can_send):
    people = await lookup_employees(client, [employee_id, registrant_id])
    message, recipients = build_message(people[employee_id], people[registrant_id], question, answer)
    if not can_send():
        return "skipped_cancelled"
    await asyncio.to_thread(smtp_send, message, recipients)
    return "sent"
