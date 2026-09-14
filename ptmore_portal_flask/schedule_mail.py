"""이전 메일 메시지 생성 호환 도구. 실제 발송은 독립 Worker의 mail_delivery.py 담당."""
import re
from email.message import EmailMessage

SENDER = "ptmorepkg.bot@sk.com"


def parse_recipients(value):
    entries = re.split(r"[;,\s]+", value) if isinstance(value, str) else (value or [])
    result = []
    for entry in entries:
        address = str(entry).strip()
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", address):
            raise ValueError("메일 주소 형식을 확인해 주세요.")
        if address.lower() not in {item.lower() for item in result}:
            result.append(address)
    if len(result) > 100:
        raise ValueError("메일 수신자는 최대 100명입니다.")
    return result


def build_schedule_email(recipients, question, answer):
    addresses = parse_recipients(recipients)
    if not addresses:
        raise ValueError("메일 수신자를 입력해 주세요.")
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = ", ".join(addresses)
    message["Subject"] = "PTMORE PKG Agent 스케줄링 실행 결과"
    message.set_content(f"안녕하세요! PTMORE PKG Agent 스케줄링 실행 결과입니다.\n실행 질문: {question}\n\n{answer}")
    return message


def send_schedule_email(recipients, question, answer, *, transport=None):
    """승인된 운영 발송 함수를 transport로 전달해야 실제 발송됩니다."""
    message = build_schedule_email(recipients, question, answer)
    if transport is None:
        raise RuntimeError("운영 메일 발송 코드가 아직 연결되지 않았습니다.")
    return transport(message)
