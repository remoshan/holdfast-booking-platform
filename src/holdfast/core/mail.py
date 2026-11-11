"""Sending email over SMTP."""

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

SMTP_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class Mailer:
    """Delivers plain-text email through an SMTP server that asks for no login, such as the
    local mail catcher."""

    host: str
    port: int
    sender: str

    async def send(self, to: str, subject: str, body: str, reference: str) -> None:
        """Deliver one email. An email sent again with the same `reference` carries the same
        Message-ID, so a mail system can recognise it as a repeat."""
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = f"<{reference}@holdfast>"
        message.set_content(body)
        # smtplib blocks while it talks to the server, so it runs in a worker thread.
        await asyncio.to_thread(self._deliver, message)

    def _deliver(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.send_message(message)
