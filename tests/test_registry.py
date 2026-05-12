"""Tests for the repos.yaml registry loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from auto_bug_fixer.registry import RegistryError, load_registry


def _write(path: Path, contents: str) -> Path:
    path.write_text(contents, encoding="utf-8")
    return path


def test_loads_minimal_valid_registry(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/acme/widgets
    default_branch: main
""",
    )
    registry = load_registry(path)
    assert len(registry) == 1
    entry = registry.entries[0]
    assert entry.url == "https://github.com/acme/widgets"
    assert entry.default_branch == "main"
    assert entry.coords.owner == "acme"
    assert entry.coords.name == "widgets"
    assert entry.slug == "acme__widgets"


def test_supports_optional_metadata(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/acme/widgets
    default_branch: main
    language: python
    test_command: pytest -q
    description: |
      multi-line
      description
""",
    )
    entry = load_registry(path).entries[0]
    assert entry.language == "python"
    assert entry.test_command == "pytest -q"
    assert entry.description is not None
    assert "multi-line" in entry.description


def test_lookup_by_url_handles_dot_git_and_trailing_slash(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/acme/widgets
    default_branch: main
""",
    )
    registry = load_registry(path)
    assert registry.by_url("https://github.com/acme/widgets") is not None
    assert registry.by_url("https://github.com/acme/widgets.git") is not None
    assert registry.by_url("https://github.com/acme/widgets/") is not None
    assert registry.by_url("https://github.com/acme/other") is None


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "missing.yaml")


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = _write(tmp_path / "repos.yaml", "repos: [unterminated")
    with pytest.raises(RegistryError, match="YAML parse error"):
        load_registry(path)


def test_missing_repos_key_raises(tmp_path: Path) -> None:
    path = _write(tmp_path / "repos.yaml", "wrong_key: 1\n")
    with pytest.raises(RegistryError, match="top-level 'repos'"):
        load_registry(path)


def test_empty_repos_list_raises(tmp_path: Path) -> None:
    path = _write(tmp_path / "repos.yaml", "repos: []\n")
    with pytest.raises(RegistryError, match="at least one entry"):
        load_registry(path)


def test_missing_url_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        "repos:\n  - default_branch: main\n",
    )
    with pytest.raises(RegistryError, match=r"repos\[0\].url is required"):
        load_registry(path)


def test_missing_default_branch_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        "repos:\n  - url: https://github.com/a/b\n",
    )
    with pytest.raises(RegistryError, match="default_branch"):
        load_registry(path)


def test_invalid_github_url_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        "repos:\n  - url: [email protected]:a/b.git\n    default_branch: main\n",
    )
    with pytest.raises(RegistryError, match="invalid"):
        load_registry(path)


def test_duplicate_url_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/a/b
    default_branch: main
  - url: https://github.com/a/b
    default_branch: dev
""",
    )
    with pytest.raises(RegistryError, match="duplicates"):
        load_registry(path)


def test_iteration_yields_all_entries(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/a/b
    default_branch: main
  - url: https://github.com/c/d
    default_branch: develop
""",
    )
    registry = load_registry(path)
    urls = [e.url for e in registry]
    assert urls == ["https://github.com/a/b", "https://github.com/c/d"]


def test_loads_display_names_and_forbidden_paths(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/talya-debug/argaman-new
    default_branch: master
    framework: react
    display_names:
      - "ארגמן"
      - "Argaman"
    forbidden_paths:
      - .env
      - firebase.js
""",
    )
    entry = load_registry(path).entries[0]
    assert entry.framework == "react"
    assert entry.display_names == ("ארגמן", "Argaman")
    assert entry.forbidden_paths == (".env", "firebase.js")


def test_lookup_by_display_name_is_case_and_whitespace_insensitive(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/talya-debug/argaman-new
    default_branch: master
    display_names: ["Argaman", "ארגמן"]
""",
    )
    registry = load_registry(path)
    assert registry.by_display_name("argaman") is not None
    assert registry.by_display_name("  ARGAMAN  ") is not None
    assert registry.by_display_name("ארגמן") is not None
    assert registry.by_display_name("nope") is None
    assert registry.by_display_name("") is None


def test_display_names_must_be_list_of_strings(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "repos.yaml",
        """
repos:
  - url: https://github.com/a/b
    default_branch: main
    display_names: "not-a-list"
""",
    )
    with pytest.raises(RegistryError, match="display_names must be a list"):
        load_registry(path)
