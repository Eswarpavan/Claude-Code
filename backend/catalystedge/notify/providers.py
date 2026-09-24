"""Email providers. Chosen from the environment: Resend, then SendGrid, then SMTP.
With none configured, messages stay queued with a clear "no provider" note."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import httpx

from catalystedge.config import Settings


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str


class EmailProvider(Protocol):
    name: str

    def send(self, msg: Email) -> None: ...


class ResendProvider:
    name = "resend"

    def __init__(self, api_key: str, sender: str, transport: httpx.BaseTransport | None = None):
        self._key, self.sender = api_key, sender
        self._client = httpx.Client(transport=transport, timeout=20)

    def send(self, msg: Email) -> None:
        r = self._client.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {self._key}"},
                              json={"from": self.sender, "to": [msg.to], "subject": msg.subject, "text": msg.text,
                                    "html": msg.html})
        if r.status_code >= 300:
            raise RuntimeError(f"resend HTTP {r.status_code}: {r.text[:160].replace(self._key, '***')}")


class SendGridProvider:
    name = "sendgrid"

    def __init__(self, api_key: str, sender: str, transport: httpx.BaseTransport | None = None):
        self._key, self.sender = api_key, sender
        self._client = httpx.Client(transport=transport, timeout=20)

    def send(self, msg: Email) -> None:
        r = self._client.post("https://api.sendgrid.com/v3/mail/send",
                              headers={"Authorization": f"Bearer {self._key}"},
                              json={"personalizations": [{"to": [{"email": msg.to}]}], "from": {"email": self.sender},
                                    "subject": msg.subject, "content": [{"type": "text/plain", "value": msg.text},
                                                                        {"type": "text/html", "value": msg.html}]})
        if r.status_code >= 300:
            raise RuntimeError(f"sendgrid HTTP {r.status_code}: {r.text[:160].replace(self._key, '***')}")


class SmtpProvider:
    name = "smtp"

    def __init__(self, host: str, port: int, user: str | None, password: str | None, sender: str):
        self.host, self.port, self.user, self._password, self.sender = host, port, user, password, sender

    def send(self, msg: Email) -> None:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.sender, msg.to, msg.subject
        m.set_content(msg.text)
        m.add_alternative(msg.html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            if self.user and self._password:
                s.login(self.user, self._password)
            s.send_message(m)


def build_provider(settings: Settings) -> EmailProvider | None:
    sender = settings.email_from or "CatalystEdge <onboarding@resend.dev>"
    if settings.resend_api_key:
        return ResendProvider(settings.resend_api_key, sender)
    if settings.sendgrid_api_key and settings.email_from:
        return SendGridProvider(settings.sendgrid_api_key, settings.email_from)
    if settings.smtp_host and settings.email_from:
        return SmtpProvider(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password,
                            settings.email_from)
    return None
