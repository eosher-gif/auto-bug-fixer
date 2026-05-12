"""Tests for env-driven Settings."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from auto_bug_fixer.config import Settings


REQUIRED_ENV = {
    "ANTHROPIC_API_KEY": "sk-test",
    "FIREBASE_PROJECT_ID": "service-tickets-cb56a",
    "FIREBASE_API_KEY": "AIzaSyDtest",
    "GITHUB_TOKEN": "tkn",
    # SMTP intentionally absent — opt-in via EMAIL_ENABLED.
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for key, value in {**REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_loads_required_env(env_isolation, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.anthropic_api_key.get_secret_value() == "sk-test"
    assert settings.firebase_project_id == "service-tickets-cb56a"
    assert settings.firebase_api_key.get_secret_value() == "AIzaSyDtest"
    assert settings.firestore_collection == "tickets"
    assert settings.bug_status_new == "open"
    assert settings.poll_interval_seconds == 30


def test_missing_required_field_raises(env_isolation) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_overrides_via_env(env_isolation, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, POLL_INTERVAL_SECONDS="7", MAX_BUGS_PER_RUN="11")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.poll_interval_seconds == 7
    assert settings.max_bugs_per_run == 11


def test_invalid_int_range_rejected(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, POLL_INTERVAL_SECONDS="0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_health_port_must_be_in_range(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, HEALTH_PORT="0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_email_disabled_by_default(env_isolation, monkeypatch: pytest.MonkeyPatch) -> None:
    """Out of the box, email is off so SMTP_* may stay blank."""
    _set_env(monkeypatch)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.email_enabled is False
    assert s.smtp_host == ""


def test_email_enabled_requires_smtp_host(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(
        monkeypatch,
        EMAIL_ENABLED="true",
        SMTP_USERNAME="u",
        SMTP_PASSWORD="p",
        NOTIFY_FROM="bot" + "@" + "host",
    )
    with pytest.raises(ValidationError, match="smtp_host"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_email_enabled_requires_notify_from(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(
        monkeypatch,
        EMAIL_ENABLED="true",
        SMTP_HOST="smtp.example",
        SMTP_USERNAME="u",
        SMTP_PASSWORD="p",
    )
    with pytest.raises(ValidationError, match="notify_from"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_email_enabled_with_full_smtp_succeeds(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(
        monkeypatch,
        EMAIL_ENABLED="true",
        SMTP_HOST="smtp.example",
        SMTP_USERNAME="u",
        SMTP_PASSWORD="p",
        NOTIFY_FROM="bot" + "@" + "host",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.email_enabled is True
    assert s.smtp_host == "smtp.example"


def test_firebase_project_id_required(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("FIREBASE_API_KEY", "k")
    with pytest.raises(ValidationError, match="FIREBASE_PROJECT_ID"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_firebase_api_key_required(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "p")
    with pytest.raises(ValidationError, match="FIREBASE_API_KEY"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_firestore_base_url_overridable_for_emulator(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, FIRESTORE_BASE_URL="http://localhost:8080/v1")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.firestore_base_url == "http://localhost:8080/v1"
