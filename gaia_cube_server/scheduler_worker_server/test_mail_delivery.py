import asyncio
import json
import pytest
import httpx
import mail_delivery as mail
import scheduler_worker as worker_module
from test_standalone_worker import _schedule, _settings, _worker_settings, _Repository, UTC
from datetime import datetime


def test_hapi_contract_and_individual_smtp(monkeypatch):
    monkeypatch.setenv("PTMORE_EMPLOYEE_HAPI_URL", "https://directory.example.test")
    monkeypatch.setenv("PTMORE_EMPLOYEE_HAPI_TOKEN", "test-token")
    observed = {}
    def handler(request):
        assert request.headers["h-api-token"] == "test-token"
        assert json.loads(request.content) == {"bindParams": ["2011111,2069026"]}
        return httpx.Response(200, json=[{"EMPNO": 2011111, "EMAIL": "recipient@sk.com", "EMP_NM": "대상자"},
                                          {"EMPNO": "2069026", "EMAIL": "registrant@sk.com", "EMP_NM": "등록자"}])
    class SMTP:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("exhub.skhynix.com", 25, 10)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def sendmail(self, sender, recipients, message):
            observed.update(sender=sender, recipients=recipients, message=message)
            return {}
    monkeypatch.setattr(mail.smtplib, "SMTP", SMTP)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await mail.send_employee_mail(client, "2011111", "2069026", "질문", "답변", can_send=lambda: True) == "sent"
    asyncio.run(run())
    assert observed["sender"] == "ptmorepkg.bot@sk.com"
    assert observed["recipients"] == ["recipient@sk.com", "registrant@sk.com"]
    assert "Cc:" in observed["message"]


@pytest.mark.parametrize("cube,enabled_mail,mail_failure", [(True, True, False), (False, True, False), (True, False, False), (True, True, True)])
def test_one_gaia_call_and_per_channel_history(monkeypatch, cube, enabled_mail, mail_failure):
    now = datetime(2026, 8, 31, tzinfo=UTC)
    document = {**_schedule(), "registrant_id": "2069026", "cube_enabled": cube, "mail_enabled": enabled_mail}
    repository = _Repository()
    worker = worker_module.SchedulerWorker(gaia_settings=_settings(), scheduler_settings=_worker_settings(), repository=repository, clock=lambda: now)
    calls = {"gaia": 0, "cube": 0, "mail": 0}
    async def gaia(*args, **kwargs): calls["gaia"] += 1; return {}
    async def send_cube(*args, **kwargs): calls["cube"] += 1
    async def send_mail(client, employee, registrant, question, answer, **kwargs):
        calls["mail"] += 1
        assert (employee, registrant, answer) == ("2011111", "2069026", "한 번 생성한 답변")
        assert kwargs["can_send"]()
        if mail_failure: raise mail.MailDeliveryError("employee_email_not_found")
        return "sent"
    monkeypatch.setattr(worker_module, "call_gaia", gaia)
    monkeypatch.setattr(worker_module, "extract_final_answer", lambda _: "한 번 생성한 답변")
    monkeypatch.setattr(worker_module, "send_cube_message", send_cube)
    monkeypatch.setattr(worker_module, "send_employee_mail", send_mail)
    asyncio.run(worker.execute_claimed_schedule(worker_module.ClaimedSchedule(document, "token", now, now), object()))
    assert calls == {"gaia": 1, "cube": int(cube), "mail": int(enabled_mail)}
    result = repository.finished[-1]
    assert result["channel_delivery"]["cube"] == ("sent" if cube else "disabled")
    assert result["delivery_status"] == ("partial_delivery" if mail_failure else "answer_sent")


def test_missing_mapping_and_cancel_do_not_send(monkeypatch):
    monkeypatch.setenv("PTMORE_EMPLOYEE_HAPI_URL", "https://directory.example.test")
    monkeypatch.setenv("PTMORE_EMPLOYEE_HAPI_TOKEN", "test-token")
    def smtp(*args): pytest.fail("must not send")
    monkeypatch.setattr(mail, "smtp_send", smtp)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))) as client:
            with pytest.raises(mail.MailDeliveryError, match="employee_email_not_found"):
                await mail.send_employee_mail(client, "2011111", "2069026", "q", "a", can_send=lambda: True)
        data = [{"EMPNO":"2011111", "EMAIL":"same@sk.com"}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as client:
            assert await mail.send_employee_mail(client, "2011111", "2011111", "q", "a", can_send=lambda: False) == "skipped_cancelled"
    asyncio.run(run())
