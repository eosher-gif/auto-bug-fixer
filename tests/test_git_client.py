"""Tests for GitClient using subprocess mocking (no real git invocations)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from auto_bug_fixer.git_ops import repo as repo_module
from auto_bug_fixer.git_ops.repo import GitClient, GitOperationError


def _client() -> GitClient:
    return GitClient(
        committer_name="bot",
        committer_email="bot" + "@" + "host",
        github_token="tkn",
        timeout_seconds=5,
    )


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _fail() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="boom"
    )


def test_with_token_rejects_ssh_remote() -> None:
    with pytest.raises(GitOperationError, match="HTTPS"):
        _client()._with_token("git@github" + ".com:a/b.git")  # noqa: SLF001


def test_with_token_injects_token_into_https_url() -> None:
    out = _client()._with_token("https://github.com/a/b.git")  # noqa: SLF001
    assert out.startswith("https://x-access-token:tkn@")
    assert out.endswith("/a/b.git")


def test_clone_runs_expected_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        captured.append(argv)
        return _ok()

    monkeypatch.setattr(repo_module.subprocess, "run", fake_run)
    dest = tmp_path / "x"
    _client().clone("https://github.com/a/b", "main", dest)

    flat = [" ".join(c) for c in captured]
    assert any(c.startswith("git clone --depth 1 --branch main") for c in flat)
    assert any("git config user.name bot" in c for c in flat)


def test_failed_command_raises_git_operation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repo_module.subprocess, "run", lambda *a, **kw: _fail()
    )
    with pytest.raises(GitOperationError, match="failed"):
        _client().clone("https://github.com/a/b", "main", tmp_path / "x")


def test_commit_all_returns_false_when_nothing_to_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence: list[subprocess.CompletedProcess] = [
        _ok(),  # git add -A
        _ok(stdout=""),  # git status --porcelain (empty)
    ]

    def fake_run(*_a: Any, **_kw: Any) -> subprocess.CompletedProcess:
        return sequence.pop(0)

    monkeypatch.setattr(repo_module.subprocess, "run", fake_run)
    assert _client().commit_all(tmp_path, "msg") is False


def test_commit_all_returns_true_when_status_nonempty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence: list[subprocess.CompletedProcess] = [
        _ok(),  # git add -A
        _ok(stdout="M  file.py\n"),  # git status
        _ok(),  # git commit
    ]

    def fake_run(*_a: Any, **_kw: Any) -> subprocess.CompletedProcess:
        return sequence.pop(0)

    monkeypatch.setattr(repo_module.subprocess, "run", fake_run)
    assert _client().commit_all(tmp_path, "msg") is True
    assert sequence == []


def test_command_timeout_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_a: Any, **_kw: Any) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=1)

    monkeypatch.setattr(repo_module.subprocess, "run", fake_run)
    with pytest.raises(GitOperationError, match="timed out"):
        _client().clone("https://github.com/a/b", "main", tmp_path / "x")
