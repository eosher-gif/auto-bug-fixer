"""Tests for RepoIndexBuilder against a synthetic repo tree."""
from __future__ import annotations

from pathlib import Path

import pytest

from auto_bug_fixer.indexer.repo_index import (
    MAX_README_BYTES,
    MAX_TREE_ENTRIES,
    RepoIndex,
    RepoIndexBuilder,
)


def test_builds_index_with_detected_language(fake_repo_tree: Path) -> None:
    index = RepoIndexBuilder().build(
        url="https://github.com/acme/widgets",
        default_branch="main",
        repo_root=fake_repo_tree,
        description=None,
        explicit_language=None,
        explicit_test_command=None,
    )
    assert index.detected_language == "python"
    assert index.suggested_test_command == "pytest -q"
    assert "README.md" in index.key_files
    assert "pyproject.toml" in index.key_files


def test_explicit_metadata_wins(fake_repo_tree: Path) -> None:
    index = RepoIndexBuilder().build(
        url="https://github.com/acme/widgets",
        default_branch="main",
        repo_root=fake_repo_tree,
        description="hand-written description",
        explicit_language="rust",
        explicit_test_command="cargo test",
    )
    assert index.detected_language == "rust"
    assert index.suggested_test_command == "cargo test"
    assert index.description == "hand-written description"


def test_skip_dirs_are_omitted_from_tree(fake_repo_tree: Path) -> None:
    index = RepoIndexBuilder().build(
        url="https://github.com/acme/widgets",
        default_branch="main",
        repo_root=fake_repo_tree,
        description=None,
        explicit_language=None,
        explicit_test_command=None,
    )
    assert all("node_modules" not in entry for entry in index.tree)
    assert any(entry.startswith("src/") for entry in index.tree)
    assert any(entry.startswith("tests/") for entry in index.tree)


def test_readme_excerpt_is_capped(tmp_path: Path) -> None:
    root = tmp_path / "huge"
    root.mkdir()
    (root / "README.md").write_text("x" * (MAX_README_BYTES + 1000), encoding="utf-8")
    index = RepoIndexBuilder().build(
        url="https://github.com/a/b",
        default_branch="main",
        repo_root=root,
        description=None,
        explicit_language=None,
        explicit_test_command=None,
    )
    assert len(index.readme_excerpt) <= MAX_README_BYTES


def test_tree_truncation_marker(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    for i in range(MAX_TREE_ENTRIES + 50):
        (root / f"file_{i:04d}.txt").write_text("x", encoding="utf-8")
    index = RepoIndexBuilder().build(
        url="https://github.com/a/b",
        default_branch="main",
        repo_root=root,
        description=None,
        explicit_language=None,
        explicit_test_command=None,
    )
    assert len(index.tree) <= MAX_TREE_ENTRIES + 1
    assert any("truncated" in entry for entry in index.tree)


def test_to_prompt_block_contains_essentials(fake_repo_tree: Path) -> None:
    index = RepoIndexBuilder().build(
        url="https://github.com/acme/widgets",
        default_branch="main",
        repo_root=fake_repo_tree,
        description="cool service",
        explicit_language=None,
        explicit_test_command=None,
    )
    block = index.to_prompt_block()
    assert "https://github.com/acme/widgets" in block
    assert "main" in block
    assert "python" in block
    assert "Fix fast" in block


def test_roundtrip_dict_preserves_fields(fake_repo_tree: Path) -> None:
    original = RepoIndexBuilder().build(
        url="https://github.com/a/b",
        default_branch="main",
        repo_root=fake_repo_tree,
        description=None,
        explicit_language=None,
        explicit_test_command=None,
    )
    restored = RepoIndex.from_dict(original.to_dict())
    assert restored == original


def test_missing_repo_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        RepoIndexBuilder().build(
            url="https://github.com/a/b",
            default_branch="main",
            repo_root=tmp_path / "does_not_exist",
            description=None,
            explicit_language=None,
            explicit_test_command=None,
        )
