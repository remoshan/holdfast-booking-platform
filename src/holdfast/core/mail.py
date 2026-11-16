"""Sending email over SMTP."""

import asyncio
import smtplib
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate

SMTP_TIMEOUT_SECONDS = 10
"""Applies to connecting and to each reply separately, so a mail server that stops answering
before it accepts the email fails the attempt. A whole send waits for about eight replies, so it
can take several times longer; the worker's idle-in-transaction limit is derived from this value
to allow for that."""


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
        # RFC 5322 requires a date, and neither EmailMessage nor smtplib adds one.
        message["Date"] = formatdate(usegmt=True)
        message["Message-ID"] = message_id
        message.set_content(body)
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        smtp = smtplib.SMTP(self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS)
        try:
            smtp.send_message(message)
            # The server has accepted the email, so nothing after this may fail the attempt, or
            # the job would send the email again. That rules out a `with` block, whose exit
            # raises when QUIT gets any reply but 221. quit() ignores the reply, and suppress()
            # covers a server that hangs up or stops answering; smtplib's errors are OSErrors.
            with suppress(OSError):
                smtp.quit()
        finally:
            smtp.close()
