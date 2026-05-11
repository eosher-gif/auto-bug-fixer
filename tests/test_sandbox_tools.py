"""Boundary tests for SandboxedFileTools and helpers (no network, no DB)."""
from __future__ import annotations

from pathlib import Path

import pytest

from auto_bug_fixer.claude_agent.tools import SandboxedFileTools, ToolError
from auto_bug_fixer.git_ops.repo import GitOperationError, parse_github_url
from auto_bug_fixer.pipeline import _branch_name


def test_sandbox_blocks_path_escape(tmp_path: Path) -> None:
    tools = SandboxedFileTools(repo_root=tmp_path)
    with pytest.raises(ToolError):
        tools.read_file("../etc/passwd")


def test_sandbox_blocks_absolute_path(tmp_path: Path) -> None:
    tools = SandboxedFileTools(repo_root=tmp_path)
    with pytest.raises(ToolError):
        tools.write_file("/tmp/evil", "x")


def test_sandbox_write_then_read_roundtrip(tmp_path: Path) -> None:
    tools = SandboxedFileTools(repo_root=tmp_path)
    tools.write_file("a/b/c.txt", "hello")
    assert tools.read_file("a/b/c.txt") == "hello"
    assert tools.changed_files == ["a/b/c.txt"]


def test_sandbox_run_cmd_rejects_unallowed() -> None:
    tools = SandboxedFileTools(repo_root=Path("/").resolve())
    with pytest.raises(ToolError):
        tools.run_cmd("rm -rf /")


def test_parse_github_url_strips_dot_git() -> None:
    coords = parse_github_url("https://github.com/acme/widgets.git")
    assert coords.owner == "acme"
    assert coords.name == "widgets"


def test_parse_github_url_rejects_ssh() -> None:
    with pytest.raises(GitOperationError):
        parse_github_url("[email protected]:acme/widgets.git")


def test_branch_name_sanitization() -> None:
    name = _branch_name("BUG/123 weird*")
    assert name.startswith("auto-bug-fixer/bug-BUG-123-weird-")
    assert " " not in name
    assert "*" not in name
    assert "/" in name
    assert name.count("/") == 1


def test_list_dir_lists_children(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b.txt").write_text("hi", encoding="utf-8")
    out = SandboxedFileTools(repo_root=tmp_path).list_dir(".")
    assert "a/" in out
    assert "b.txt" in out


def test_list_dir_rejects_non_directory(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ToolError, match="not a directory"):
        SandboxedFileTools(repo_root=tmp_path).list_dir("x.txt")


def test_read_file_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="not a file"):
        SandboxedFileTools(repo_root=tmp_path).read_file("missing.txt")


def test_read_file_rejects_oversize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_bug_fixer.claude_agent import tools as tools_mod

    monkeypatch.setattr(tools_mod, "MAX_READ_BYTES", 10)
    big = tmp_path / "big.txt"
    big.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(ToolError, match="too large"):
        SandboxedFileTools(repo_root=tmp_path).read_file("big.txt")


def test_run_cmd_allows_ls(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    output = SandboxedFileTools(repo_root=tmp_path).run_cmd("ls")
    assert "f.txt" in output
    assert "exit_code=" in output


def test_changed_files_uses_posix_paths(tmp_path: Path) -> None:
    tools = SandboxedFileTools(repo_root=tmp_path)
    tools.write_file("a/b/c.txt", "x")
    tools.write_file("a/d.txt", "y")
    assert tools.changed_files == ["a/b/c.txt", "a/d.txt"]
