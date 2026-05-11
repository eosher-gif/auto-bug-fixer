"""Tests for the argparse CLI dispatcher."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from auto_bug_fixer import cli


REQUIRED_ENV = {
    "ANTHROPIC_API_KEY": "x",
    "DATABASE_URL": "sqlite:///:memory:",
    "GITHUB_TOKEN": "x",
    "SMTP_HOST": "h",
    "SMTP_USERNAME": "u",
    "SMTP_PASSWORD": "p",
    "NOTIFY_FROM": "bot" + "@" + "host",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for k, v in {**REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(k, v)
    cli.get_settings.cache_clear()


def test_unknown_command_prints_help_and_returns_2(
    env_isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 2


def test_run_once_invokes_pipeline(
    env_isolation, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _set_env(
        monkeypatch,
        REPOS_FILE=str(tmp_path / "no.yaml"),
        INDEX_DIR=str(tmp_path / "idx"),
    )
    monkeypatch.setattr(cli, "BugFixPipeline", _StubPipeline)
    rc = cli.main(["run-once"])
    assert rc == 0
    assert _StubPipeline.calls == 1


def test_index_once_returns_1_when_registry_missing(
    env_isolation, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _set_env(
        monkeypatch,
        REPOS_FILE=str(tmp_path / "no.yaml"),
        INDEX_DIR=str(tmp_path / "idx"),
    )
    rc = cli.main(["index-once"])
    assert rc == 1


def test_index_once_runs_runner_when_registry_present(
    env_isolation, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    repos = tmp_path / "repos.yaml"
    repos.write_text(
        "repos:\n"
        "  - url: https://github.com/a/b\n"
        "    default_branch: main\n",
        encoding="utf-8",
    )
    _set_env(monkeypatch, REPOS_FILE=str(repos), INDEX_DIR=str(tmp_path / "idx"))

    class _FakeRunner:
        runs = 0

        def __init__(self, *a, **kw): ...

        def index_all(self):
            _FakeRunner.runs += 1
            return 1

    monkeypatch.setattr(cli, "IndexRunner", _FakeRunner)
    rc = cli.main(["index-once"])
    assert rc == 0
    assert _FakeRunner.runs == 1


def test_daemon_starts_and_stops(
    env_isolation, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _set_env(
        monkeypatch,
        REPOS_FILE=str(tmp_path / "no.yaml"),
        INDEX_DIR=str(tmp_path / "idx"),
        HEALTH_ENABLED="false",
    )
    monkeypatch.setattr(cli, "BugFixPipeline", _StubPipeline)

    started = {"value": False}

    class _StubDaemon:
        def __init__(self, **kw):
            self.kw = kw

        def install_signal_handlers(self):
            pass

        def run_forever(self):
            started["value"] = True
            return 0

    monkeypatch.setattr(cli, "BugFixDaemon", _StubDaemon)
    rc = cli.main(["daemon"])
    assert rc == 0
    assert started["value"] is True


class _StubPipeline:
    calls = 0

    def __init__(self, *a, **kw):
        pass

    def run_once(self):
        _StubPipeline.calls += 1
        return 0


@pytest.fixture(autouse=True)
def _clear_pipeline_counter():
    _StubPipeline.calls = 0
    yield
