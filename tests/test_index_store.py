"""Tests for IndexStore: roundtrip, corruption handling, and lookup."""
from __future__ import annotations

from pathlib import Path

from auto_bug_fixer.indexer.index_store import IndexStore
from auto_bug_fixer.indexer.repo_index import RepoIndex
from auto_bug_fixer.registry import RegistryEntry


def _entry(url: str = "https://github.com/acme/widgets") -> RegistryEntry:
    return RegistryEntry(
        url=url,
        default_branch="main",
        language=None,
        test_command=None,
        description=None,
    )


def _index(url: str = "https://github.com/acme/widgets") -> RepoIndex:
    return RepoIndex(
        url=url,
        default_branch="main",
        indexed_at="2026-01-01T00:00:00+00:00",
        detected_language="python",
        suggested_test_command="pytest -q",
        description="d",
        readme_excerpt="readme",
        tree=["src/", "src/main.py"],
        key_files=["pyproject.toml"],
    )


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    store = IndexStore(base_dir=tmp_path / "indices")
    entry = _entry()
    saved_path = store.save(entry, _index())
    assert saved_path.exists()
    loaded = store.load(entry)
    assert loaded == _index()


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    store = IndexStore(base_dir=tmp_path / "indices")
    assert store.load(_entry()) is None


def test_load_handles_corrupt_file(tmp_path: Path) -> None:
    store = IndexStore(base_dir=tmp_path / "indices")
    entry = _entry()
    store.save(entry, _index())
    store.path_for(entry).write_text("{ this is not json", encoding="utf-8")
    assert store.load(entry) is None


def test_path_for_uses_owner_name_slug(tmp_path: Path) -> None:
    store = IndexStore(base_dir=tmp_path / "indices")
    entry = _entry("https://github.com/acme/widgets.git")
    assert store.path_for(entry).name == "acme__widgets.json"


def test_save_creates_directory(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    store = IndexStore(base_dir=deep)
    store.save(_entry(), _index())
    assert deep.exists()


def test_load_by_url_uses_lookup(tmp_path: Path) -> None:
    store = IndexStore(base_dir=tmp_path / "indices")
    entry = _entry()
    store.save(entry, _index())

    def lookup(url: str) -> RegistryEntry | None:
        return entry if url == entry.url else None

    assert store.load_by_url(entry.url, lookup) == _index()
    assert store.load_by_url("https://github.com/x/y", lookup) is None
