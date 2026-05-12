"""Tests for ClaudeBugFixerAgent using a fake Anthropic client."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from auto_bug_fixer.claude_agent import agent as agent_module
from auto_bug_fixer.claude_agent.agent import ClaudeBugFixerAgent
from auto_bug_fixer.config import Settings
from auto_bug_fixer.models import Bug


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_api_key="x",
        firebase_project_id="proj",
        firebase_api_key="key",
        github_token="x",
        claude_max_tool_iterations=4,
    )


def _bug() -> Bug:
    return Bug(
        id="B1",
        title="t",
        description="d",
        repo_url="https://github.com/a/b",
        base_branch="main",
        reporter_email=None,
    )


@dataclass
class _FakeBlock:
    type: str
    name: str = ""
    input: dict[str, Any] | None = None
    id: str = "tu_1"


@dataclass
class _FakeResponse:
    content: list[_FakeBlock]


class _FakeMessages:
    def __init__(self, sequence: list[_FakeResponse]) -> None:
        self.sequence = sequence
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self.sequence:
            raise AssertionError("model invoked more times than expected")
        return self.sequence.pop(0)


class _FakeAnthropic:
    def __init__(self, sequence: list[_FakeResponse]) -> None:
        self.messages = _FakeMessages(sequence)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, sequence: list[_FakeResponse]
) -> _FakeAnthropic:
    fake = _FakeAnthropic(sequence)
    monkeypatch.setattr(agent_module, "Anthropic", lambda **_kw: fake)
    return fake


def test_agent_writes_then_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = [
        _FakeResponse(
            content=[
                _FakeBlock(
                    type="tool_use",
                    name="write_file",
                    input={"path": "a.txt", "content": "hi"},
                    id="t1",
                )
            ]
        ),
        _FakeResponse(
            content=[
                _FakeBlock(
                    type="tool_use",
                    name="finish",
                    input={"summary": "done"},
                    id="t2",
                )
            ]
        ),
    ]
    _patch_client(monkeypatch, sequence)
    outcome = ClaudeBugFixerAgent(_settings()).fix_bug(_bug(), tmp_path)
    assert outcome.success is True
    assert outcome.summary == "done"
    assert outcome.changed_files == ["a.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi"


def test_agent_returns_failure_when_finishes_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = [
        _FakeResponse(
            content=[
                _FakeBlock(
                    type="tool_use",
                    name="finish",
                    input={"summary": "nothing to do"},
                    id="t1",
                )
            ]
        )
    ]
    _patch_client(monkeypatch, sequence)
    outcome = ClaudeBugFixerAgent(_settings()).fix_bug(_bug(), tmp_path)
    assert outcome.success is False
    assert outcome.error == "agent finished without changing any file"


def test_agent_handles_unknown_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = [
        _FakeResponse(
            content=[
                _FakeBlock(
                    type="tool_use",
                    name="unknown_tool",
                    input={},
                    id="t1",
                )
            ]
        ),
        _FakeResponse(
            content=[
                _FakeBlock(
                    type="tool_use",
                    name="finish",
                    input={"summary": "gave up"},
                    id="t2",
                )
            ]
        ),
    ]
    _patch_client(monkeypatch, sequence)
    outcome = ClaudeBugFixerAgent(_settings()).fix_bug(_bug(), tmp_path)
    assert outcome.success is False


def test_agent_returns_failure_when_no_tool_use_in_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = [_FakeResponse(content=[_FakeBlock(type="text")])]
    _patch_client(monkeypatch, sequence)
    outcome = ClaudeBugFixerAgent(_settings()).fix_bug(_bug(), tmp_path)
    assert outcome.success is False
    assert outcome.error == "no tool use in response"


def test_agent_hits_iteration_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_write = _FakeResponse(
        content=[
            _FakeBlock(
                type="tool_use",
                name="list_dir",
                input={"path": "."},
                id="t",
            )
        ]
    )
    sequence = [one_write] * 4
    _patch_client(monkeypatch, sequence)
    outcome = ClaudeBugFixerAgent(_settings()).fix_bug(_bug(), tmp_path)
    assert outcome.success is False
    assert "max_iterations" in (outcome.error or "")
