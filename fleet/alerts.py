"""Owner alerts. Twilio for real use, Fake for tests, Null when unconfigured."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol
from xml.sax.saxutils import escape

log = logging.getLogger("fleet.alerts")

INFO, WARNING, CRITICAL = "info", "warning", "critical"


class Alerter(Protocol):
    async def notify(self, message: str, severity: str = WARNING) -> None: ...


class NullAlerter:
    """Used when no provider is configured: alerts are only logged."""

    async def notify(self, message: str, severity: str = WARNING) -> None:
        log.warning("alert (not delivered, no provider configured) [%s] %s", severity, message)


@dataclass
class FakeAlerter:
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def notify(self, message: str, severity: str = WARNING) -> None:
        self.sent.append((severity, message))


class TwilioAlerter:
    """SMS for every alert; additionally a voice call when severity is critical.

    Delivery failures are logged, never raised: a broken alert channel must not
    take down the worker that is trying to report a problem.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        client: object | None = None,
    ) -> None:
        if client is None:
            from twilio.rest import Client

            client = Client(account_sid, auth_token)
        self._client = client
        self._from = from_number
        self._to = to_number

    @classmethod
    def from_env(cls) -> TwilioAlerter | None:
        e = os.environ
        keys = (
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
            "FLEET_ALERT_TO_NUMBER",
        )
        if not all(e.get(k) for k in keys):
            return None
        return cls(
            e["TWILIO_ACCOUNT_SID"],
            e["TWILIO_AUTH_TOKEN"],
            e["TWILIO_FROM_NUMBER"],
            e["FLEET_ALERT_TO_NUMBER"],
        )

    def _send(self, message: str, severity: str) -> None:
        text = f"[fleet:{severity}] {message}"[:1500]
        self._client.messages.create(to=self._to, from_=self._from, body=text)
        if severity == CRITICAL:
            twiml = f"<Response><Say>{escape(text[:400])}</Say></Response>"
            self._client.calls.create(to=self._to, from_=self._from, twiml=twiml)

    async def notify(self, message: str, severity: str = WARNING) -> None:
        try:
            await asyncio.to_thread(self._send, message, severity)
        except Exception:
            log.exception("failed to deliver alert via Twilio")


def alerter_from_env() -> Alerter:
    return TwilioAlerter.from_env() or NullAlerter()
