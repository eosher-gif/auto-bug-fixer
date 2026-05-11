"""Tests for EmailNotifier using a mock SMTP client (no network)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from auto_bug_fixer.config import Settings
from auto_bug_fixer.models import Bug, FixOutcome, PullRequest
from auto_bug_fixer.notify.email_sender import EmailDeliveryError, EmailNotifier

# Build addresses at runtime to avoid the IDE auto-obfuscating literal email
# strings to placeholders (which then fail RFC validation in Python's email
# parser). Keeping these as code-level constants makes the intent explicit.
AT = "@"
SENDER = "bot" + AT + "host"
REPORTER = "user" + AT + "host"
CC1 = "ops" + AT + "host"
CC2 = "qa" + AT + "host"


@dataclass
class _SmtpCalls:
    """Records every method invoked on the fake SMTP context manager."""

    starttls: int = 0
    login: list[tuple[str, str]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)


class _FakeSmtp:
    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = _calls

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def ehlo(self) -> None:
        pass

    def starttls(self, context: Any) -> None:
        self.calls.starttls += 1

    def login(self, username: str, password: str) -> None:
        self.calls.login.append((username, password))

    def send_message(self, message: Any, to_addrs: list[str]) -> None:
        self.calls.sent.append(
            {
                "to_addrs": list(to_addrs),
                "from_header": str(message["From"]),
                "to_header": str(message["To"]),
                "cc_header": str(message["Cc"]) if message.get("Cc") else None,
                "subject": str(message["Subject"]),
                "body": message.get_content(),
            }
        )


_calls = _SmtpCalls()


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    _calls.starttls = 0
    _calls.login.clear()
    _calls.sent.clear()


def _settings(notify_cc: str = "") -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_api_key="x",
        database_url="sqlite:///:memory:",
        github_token="x",
        smtp_host="smtp.example",
        smtp_port=587,
        smtp_username="user",
        smtp_password="secret",
        notify_from=SENDER,
        notify_cc=notify_cc,
    )


def _bug(reporter: str | None = REPORTER) -> Bug:
    return Bug(
        id="B1",
        title="t",
        description="d",
        repo_url="https://github.com/a/b",
        base_branch="main",
        reporter_email=reporter,
    )


def test_notify_success_sends_message_with_pr_url() -> None:
    notifier = EmailNotifier(_settings())
    with patch("auto_bug_fixer.notify.email_sender.smtplib.SMTP", _FakeSmtp):
        notifier.notify_success(
            bug=_bug(),
            outcome=FixOutcome(success=True, summary="fixed", changed_files=["a/b.py"]),
            pr=PullRequest(
                number=42, url="https://example.com/pr/42", branch="x", title="y"
            ),
        )
    assert len(_calls.sent) == 1
    sent = _calls.sent[0]
    assert sent["to_addrs"] == [REPORTER]
    assert sent["from_header"] == SENDER
    assert sent["to_header"] == REPORTER
    assert "https://example.com/pr/42" in sent["body"]
    assert "a/b.py" in sent["body"]
    assert _calls.starttls == 1
    assert _calls.login == [("user", "secret")]


def test_notify_failure_includes_error() -> None:
    notifier = EmailNotifier(_settings())
    with patch("auto_bug_fixer.notify.email_sender.smtplib.SMTP", _FakeSmtp):
        notifier.notify_failure(
            bug=_bug(),
            outcome=FixOutcome(success=False, summary="explanation", error="root cause"),
        )
    assert len(_calls.sent) == 1
    body = _calls.sent[0]["body"]
    assert "root cause" in body
    assert "explanation" in body
    assert _calls.sent[0]["subject"].startswith("[auto-bug-fixer] Could not")


def test_no_email_when_reporter_missing() -> None:
    notifier = EmailNotifier(_settings())
    with patch("auto_bug_fixer.notify.email_sender.smtplib.SMTP", _FakeSmtp):
        notifier.notify_failure(
            bug=_bug(reporter=None),
            outcome=FixOutcome(success=False, summary="x", error="y"),
        )
    assert _calls.sent == []


def test_cc_addresses_included_in_recipients() -> None:
    notifier = EmailNotifier(_settings(notify_cc=f"{CC1}, {CC2}"))
    with patch("auto_bug_fixer.notify.email_sender.smtplib.SMTP", _FakeSmtp):
        notifier.notify_success(
            bug=_bug(),
            outcome=FixOutcome(success=True, summary="ok", changed_files=["x.py"]),
            pr=PullRequest(number=1, url="u", branch="b", title="t"),
        )
    assert _calls.sent[0]["to_addrs"] == [REPORTER, CC1, CC2]
    assert CC1 in _calls.sent[0]["cc_header"]
    assert CC2 in _calls.sent[0]["cc_header"]


def test_smtp_failure_raises_email_delivery_error() -> None:
    class _BoomSmtp(_FakeSmtp):
        def send_message(self, *_a: Any, **_kw: Any) -> None:
            import smtplib

            raise smtplib.SMTPException("boom")

    notifier = EmailNotifier(_settings())
    with patch("auto_bug_fixer.notify.email_sender.smtplib.SMTP", _BoomSmtp):
        with pytest.raises(EmailDeliveryError, match="boom"):
            notifier.notify_failure(
                bug=_bug(),
                outcome=FixOutcome(success=False, summary="x", error="y"),
            )
