"""Sending email over SMTP."""

import asyncio
import smtplib
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import formatdate

SMTP_TIMEOUT_SECONDS = 10
"""How long to wait for the mail server to accept the connection or to send its next bytes, so
a server that stops answering before it accepts the email fails the attempt."""

SEND_DEADLINE_SECONDS = 60
"""How long a send may run before it is cut off. A server that keeps answering, but slowly, never
trips the timeout above; without a deadline it could hold the worker, and every job due after
the email, for ever. Connecting cannot be cut short, but it times out for each address the
host resolves to. The worker's idle-in-transaction limit is derived from this value."""

# SMTP may carry 8-bit text only to a server that offers 8BITMIME, and smtplib asks for that
# only when an address needs SMTPUTF8. So a body that is not plain ASCII is encoded.
_SEVEN_BIT = policy.default.clone(cte_type="7bit")


@dataclass(frozen=True, slots=True)
class Mailer:
    host: str
    port: int
    sender: str

    async def send(self, recipient: str, subject: str, body: str, message_id: str) -> None:
        """Send a plain-text email. smtplib blocks, so it runs in a worker thread."""
        message = EmailMessage(policy=_SEVEN_BIT)
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        # RFC 5322 requires a date, and neither EmailMessage nor smtplib adds one.
        message["Date"] = formatdate(usegmt=True)
        message["Message-ID"] = message_id
        message.set_content(body)
        await asyncio.to_thread(self._send, message, recipient)

    def _send(self, message: EmailMessage, recipient: str) -> None:
        smtp = smtplib.SMTP(timeout=SMTP_TIMEOUT_SECONDS)
        finished = threading.Event()
        threading.Thread(target=_hang_up_at_deadline, args=(smtp, finished)).start()
        try:
            code, reply = smtp.connect(self.host, self.port)
            if code != 220:
                raise smtplib.SMTPConnectError(code, reply)
            # The recipient goes to the server as given. Left to read it from the To header,
            # smtplib would get the header parser's version, which decodes RFC 2047 encoded words
            # even inside an address: =?utf-8?q?ada?=@example.com would reach ada@example.com.
            smtp.send_message(message, to_addrs=[recipient])
            # The server has accepted the email, so nothing after this may fail the attempt, or
            # the job would send the email again. That rules out a `with` block, whose exit
            # raises when QUIT gets any reply but 221. quit() ignores the reply, and suppress()
            # covers a server that hangs up or stops answering; smtplib's errors are OSErrors.
            with suppress(OSError):
                smtp.quit()
        finally:
            finished.set()
            smtp.close()


def _hang_up_at_deadline(smtp: smtplib.SMTP, finished: threading.Event) -> None:
    """Cut the send off at its deadline, unless it finishes first. Shutting the socket down fails
    the read that waits on it, as if the server had hung up; closing it would wait for that read
    to finish. A send that is still connecting at the deadline is cut off once it has a socket."""
    if finished.wait(SEND_DEADLINE_SECONDS):
        return
    while (sock := smtp.sock) is None:
        if finished.wait(0.1):
            return
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
