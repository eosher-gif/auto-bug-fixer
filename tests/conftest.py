"""Shared fixtures and helpers for the test suite."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


GIT_AVAILABLE = _git_available()


requires_git = pytest.mark.skipif(
    not GIT_AVAILABLE,
    reason="git CLI not installed in this environment",
)


@pytest.fixture
def fake_repo_tree(tmp_path: Path) -> Path:
    """Create a small synthetic repository tree on disk and return its root."""
    root = tmp_path / "fake_repo"
    root.mkdir()
    (root / "README.md").write_text(
        "# Fake Repo\n\nA tiny synthetic repository used by tests.\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fake"\nversion = "0.0.1"\n',
        encoding="utf-8",
    )
    src = root / "src" / "fake"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('"""fake package."""\n', encoding="utf-8")
    (src / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_core.py").write_text(
        "from fake.core import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    skip = root / "node_modules"
    skip.mkdir()
    (skip / "leaf.txt").write_text("should be skipped", encoding="utf-8")
    return root


@pytest.fixture
def env_isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Wipe all auto_bug_fixer-related env vars so config tests start clean."""
    keys = [
        k
        for k in os.environ
        if k.startswith(
            (
                "ANTHROPIC_",
                "DATABASE_",
                "BUG_",
                "MAX_BUGS_",
                "GITHUB_",
                "GIT_",
                "SMTP_",
                "NOTIFY_",
                "WORKSPACE_",
                "POLL_",
                "IDLE_",
                "ERROR_",
                "REPOS_",
                "INDEX_",
                "REINDEX_",
                "HEALTH_",
                "LOG_",
                "CLAUDE_",
            )
        )
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYDANTIC_DISABLE_ENV_FILE", "1")
    yield
