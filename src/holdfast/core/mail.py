"""Sending email over SMTP."""

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

SMTP_TIMEOUT_SECONDS = 10
"""Applies to connecting and to every reply, so a mail server that stops answering fails the
attempt instead of holding the job forever."""


@dataclass(frozen=True, slots=True)
class Mailer:
    host: str
    port: int
    sender: str

    async def send(self, recipient: str, subject: str, body: str, message_id: str) -> None:
        """Send a plain-text email. smtplib blocks, so it runs in a worker thread."""
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message.set_content(body)
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.send_message(message)
