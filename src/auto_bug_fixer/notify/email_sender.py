"""SMTP email sender for fix-confirmation messages."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from auto_bug_fixer.config import Settings
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.models import Bug, FixOutcome, PullRequest

log = get_logger(__name__)

SMTP_TIMEOUT_SECONDS = 30


class EmailDeliveryError(RuntimeError):
    """Raised when SMTP delivery fails."""


class EmailNotifier:
    """Sends success / failure confirmations via SMTP."""

    def __init__(self, settings: Settings) -> None:
        """Bind a notifier to its SMTP credentials."""
        self._settings = settings

    def notify_success(
        self,
        bug: Bug,
        outcome: FixOutcome,
        pr: PullRequest,
    ) -> None:
        """Send a success email about ``pr`` to the bug reporter (if known)."""
        if not self._settings.email_enabled:
            log.info("skip_email_disabled", bug_id=bug.id)
            return
        if not bug.reporter_email:
            log.info("skip_email_no_reporter", bug_id=bug.id)
            return
        subject = f"[auto-bug-fixer] PR ready for bug {bug.id}: {bug.title}"
        body = (
            f"Hi,\n\n"
            f"An automated fix has been opened for bug {bug.id}.\n\n"
            f"Title: {bug.title}\n"
            f"Pull request: {pr.url}\n"
            f"Branch: {pr.branch}\n\n"
            f"Summary of the change:\n{outcome.summary}\n\n"
            f"Files changed:\n"
            + "\n".join(f"  - {p}" for p in outcome.changed_files)
            + "\n\nPlease review and confirm.\n"
        )
        self._send(to=bug.reporter_email, subject=subject, body=body)

    def notify_failure(self, bug: Bug, outcome: FixOutcome) -> None:
        """Send a failure email so a human knows to take over."""
        if not self._settings.email_enabled:
            log.info("skip_email_disabled", bug_id=bug.id)
            return
        if not bug.reporter_email:
            log.info("skip_email_no_reporter", bug_id=bug.id)
            return
        subject = f"[auto-bug-fixer] Could not auto-fix bug {bug.id}"
        body = (
            f"Hi,\n\n"
            f"The auto bug-fixer was unable to produce a fix for bug {bug.id}.\n\n"
            f"Title: {bug.title}\n"
            f"Reason: {outcome.error or 'unknown'}\n"
            f"Agent notes:\n{outcome.summary}\n\n"
            f"A human will need to look at this one.\n"
        )
        self._send(to=bug.reporter_email, subject=subject, body=body)

    def _send(self, *, to: str, subject: str, body: str) -> None:
        s = self._settings
        message = EmailMessage()
        message["From"] = s.notify_from
        message["To"] = to
        if s.notify_cc:
            message["Cc"] = s.notify_cc
        message["Subject"] = subject
        message.set_content(body)

        recipients = [to] + [
            addr.strip() for addr in s.notify_cc.split(",") if addr.strip()
        ]
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                smtp.send_message(message, to_addrs=recipients)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailDeliveryError(f"SMTP send failed: {exc}") from exc

        log.info("email_sent", to=to, subject=subject)
