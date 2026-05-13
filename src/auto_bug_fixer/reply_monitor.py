"""Monitor Gmail IMAP for replies to auto-bug-fixer emails.

PRIVACY: reads ONLY emails whose subject starts with
``Re: [auto-bug-fixer]``. Every other email is completely ignored.
Matching emails are marked as read (SEEN) after processing so they
are not picked up again.
"""
from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from email.header import decode_header
from email.utils import parseaddr

from auto_bug_fixer.config import Settings
from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)

SUBJECT_PREFIX = "[auto-bug-fixer]"
IMAP_PORT = 993


@dataclass(frozen=True)
class ReplyMessage:
    """A parsed reply to a bot-sent email."""

    bug_id: str
    feedback: str
    from_email: str
    subject: str
    message_id: str


class ReplyMonitor:
    """Connects to Gmail IMAP and extracts replies to bot emails."""

    def __init__(self, settings: Settings) -> None:
        self._host = _imap_host(settings.smtp_host)
        self._username = settings.smtp_username
        self._password = settings.smtp_password.get_secret_value()

    def check_replies(self) -> list[ReplyMessage]:
        """Return new replies and mark them as read.

        Only searches for ``Re: [auto-bug-fixer] ...`` subjects.
        """
        if not self._host or not self._username or not self._password:
            log.info("reply_monitor_disabled", reason="no IMAP credentials")
            return []

        try:
            return self._fetch_replies()
        except (imaplib.IMAP4.error, OSError) as exc:
            log.warning("reply_monitor_error", error=str(exc))
            return []

    def _fetch_replies(self) -> list[ReplyMessage]:
        replies: list[ReplyMessage] = []

        with imaplib.IMAP4_SSL(self._host, IMAP_PORT) as imap:
            imap.login(self._username, self._password)
            imap.select("INBOX")

            # Search for unread replies to our emails only
            search_query = '(UNSEEN SUBJECT "Re: [auto-bug-fixer]")'
            _status, msg_ids = imap.search(None, search_query)

            if not msg_ids or not msg_ids[0]:
                log.info("reply_monitor_no_replies")
                return []

            for msg_id in msg_ids[0].split():
                reply = self._parse_one(imap, msg_id)
                if reply is not None:
                    replies.append(reply)
                    # Mark as read so we don't process again
                    imap.store(msg_id, "+FLAGS", "\\Seen")

        if replies:
            log.info("reply_monitor_found", count=len(replies))
        return replies

    def _parse_one(self, imap: imaplib.IMAP4_SSL, msg_id: bytes) -> ReplyMessage | None:
        _status, data = imap.fetch(msg_id, "(RFC822)")
        if not data or not data[0] or not isinstance(data[0], tuple):
            return None

        msg = email.message_from_bytes(data[0][1])
        subject = _decode_subject(msg.get("Subject", ""))

        # Double-check: only process replies to our emails
        if SUBJECT_PREFIX not in subject:
            return None

        bug_id = _extract_bug_id(subject)
        if not bug_id:
            log.warning("reply_no_bug_id", subject=subject)
            return None

        feedback = _extract_body(msg)
        if not feedback.strip():
            log.warning("reply_empty_body", bug_id=bug_id)
            return None

        from_email = parseaddr(msg.get("From", ""))[1]
        message_id = msg.get("Message-ID", "")

        log.info(
            "reply_parsed",
            bug_id=bug_id,
            from_email=from_email,
            feedback_length=len(feedback),
        )
        return ReplyMessage(
            bug_id=bug_id,
            feedback=feedback.strip(),
            from_email=from_email,
            subject=subject,
            message_id=message_id,
        )


def _imap_host(smtp_host: str) -> str:
    """Derive IMAP host from SMTP host (smtp.gmail.com -> imap.gmail.com)."""
    if not smtp_host:
        return ""
    return smtp_host.replace("smtp.", "imap.")


def _decode_subject(raw: str) -> str:
    """Decode a possibly RFC2047-encoded subject header."""
    parts = decode_header(raw)
    decoded: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_bug_id(subject: str) -> str | None:
    """Pull the bug ID from a subject like ``Re: [auto-bug-fixer] ... באג ABC123: ...``."""
    # Pattern: "באג <ID>:" or "bug <ID>:"
    m = re.search(r"(?:באג|bug)\s+([A-Za-z0-9_-]+)", subject, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _extract_body(msg: email.message.Message) -> str:
    """Extract the reply text, stripping quoted content."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                body = part.get_payload(decode=True).decode(charset, errors="replace")
                break
    else:
        charset = msg.get_content_charset() or "utf-8"
        body = msg.get_payload(decode=True).decode(charset, errors="replace")

    # Strip quoted reply (lines starting with > or "On ... wrote:")
    lines = body.splitlines()
    reply_lines: list[str] = []
    for line in lines:
        # Stop at quoted content
        if line.strip().startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", line.strip()):
            break
        if re.match(r"^ב.+ כתב/ה:$", line.strip()):
            break
        # Stop at common separators
        if line.strip() in ("--", "---", "____"):
            break
        reply_lines.append(line)

    return "\n".join(reply_lines).strip()
