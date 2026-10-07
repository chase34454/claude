import pytest

from fleet import cli
from fleet.alerts import CRITICAL, WARNING, NullAlerter, TwilioAlerter, alerter_from_env


class FakeTwilio:
    def __init__(self, fail=False):
        self.fail, self.sms, self.calls_made = fail, [], []
        outer = self

        class M:
            def create(self, **kw):
                if outer.fail:
                    raise RuntimeError("twilio down")
                outer.sms.append(kw)

        class C:
            def create(self, **kw):
                outer.calls_made.append(kw)

        self.messages, self.calls = M(), C()


async def test_warning_sends_sms_only():
    f = FakeTwilio()
    await TwilioAlerter("s", "t", "+1", "+2", client=f).notify("hi", WARNING)
    assert len(f.sms) == 1 and not f.calls_made
    assert f.sms[0]["to"] == "+2" and f.sms[0]["from_"] == "+1"


async def test_critical_sms_and_call_escapes_xml():
    f = FakeTwilio()
    await TwilioAlerter("s", "t", "+1", "+2", client=f).notify("a <b> & c", CRITICAL)
    assert len(f.sms) == 1 and len(f.calls_made) == 1
    assert "&lt;b&gt;" in f.calls_made[0]["twiml"]


async def test_delivery_failure_does_not_raise():
    await TwilioAlerter("s", "t", "+1", "+2", client=FakeTwilio(fail=True)).notify("x", CRITICAL)


def test_from_env(monkeypatch):
    for k in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "FLEET_ALERT_TO_NUMBER",
    ):
        monkeypatch.delenv(k, raising=False)
    assert isinstance(alerter_from_env(), NullAlerter)
    for k in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "FLEET_ALERT_TO_NUMBER",
    ):
        monkeypatch.setenv(k, "x")
    assert isinstance(alerter_from_env(), TwilioAlerter)


def test_cli_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "c.db"))
    assert cli.main(["add-task", "hello", "--budget", "0.5"]) == 0
    assert "queued task 1" in capsys.readouterr().out
    from fleet.db import Store

    s = Store(str(tmp_path / "c.db"))
    aid = s.create_approval(1, "send_email", {"to": "x"})
    s.close()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "hello" in out and f"PENDING approval #{aid}" in out
    assert cli.main(["approve", str(aid)]) == 0
    assert cli.main(["approve", str(aid)]) == 1  # already decided
    assert cli.main(["status", "1"]) == 0
    assert cli.main(["status", "99"]) == 1


def test_cli_run_once_with_mock_client(tmp_path, monkeypatch, capsys):
    from conftest import FakeClient, reply, text

    from fleet.alerts import FakeAlerter

    monkeypatch.setenv("FLEET_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("FLEET_WORKSPACE", str(tmp_path / "ws"))
    cli.main(["add-task", "go"])
    client = FakeClient([reply(text("r")), reply(text("VERDICT: PASS"))])
    assert (
        cli.main(["run", "--once", "--concurrency", "1"], client=client, alerter=FakeAlerter()) == 0
    )
    capsys.readouterr()
    cli.main(["status", "1"])
    assert "done" in capsys.readouterr().out


def test_cli_requires_command():
    with pytest.raises(SystemExit):
        cli.main([])
