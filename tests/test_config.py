"""Tests for env-driven Settings."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from auto_bug_fixer.config import Settings


REQUIRED_ENV = {
    "ANTHROPIC_API_KEY": "sk-test",
    "DATABASE_URL": "sqlite:///:memory:",
    "GITHUB_TOKEN": "tkn",
    "SMTP_HOST": "smtp.example",
    "SMTP_USERNAME": "u",
    "SMTP_PASSWORD": "p",
    "NOTIFY_FROM": "[email protected]",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for key, value in {**REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_loads_required_env(env_isolation, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.anthropic_api_key.get_secret_value() == "sk-test"
    assert settings.smtp_port == 587
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
