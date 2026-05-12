"""Tests for the per-repo fix history store."""
from __future__ import annotations

from pathlib import Path

from auto_bug_fixer.indexer.history_store import HistoryEntry, HistoryStore
from auto_bug_fixer.registry import RegistryEntry


def _entry() -> RegistryEntry:
    return RegistryEntry(
        url="https://github.com/acme/widgets",
        default_branch="main",
        language="javascript",
        test_command=None,
        description=None,
        display_names=("widgets",),
    )


def _history(bug_id: str = "B-1", title: str = "fix button") -> HistoryEntry:
    return HistoryEntry(
        bug_id=bug_id,
        ticket_title=title,
        pr_url=f"https://github.com/acme/widgets/pull/1",
        pr_number=1,
        files_touched=["src/App.jsx"],
        ai_summary="Fixed the button click handler",
        ts="2026-01-01T00:00:00+00:00",
    )


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    entry = _entry()
    h = _history()
    store.append(entry, h)
    recent = store.read_recent(entry)
    assert len(recent) == 1
    assert recent[0].bug_id == "B-1"
    assert recent[0].ticket_title == "fix button"
    assert recent[0].files_touched == ["src/App.jsx"]


def test_append_multiple(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    entry = _entry()
    for i in range(5):
        store.append(entry, _history(bug_id=f"B-{i}", title=f"fix {i}"))
    recent = store.read_recent(entry)
    assert len(recent) == 5
    assert recent[0].bug_id == "B-0"
    assert recent[4].bug_id == "B-4"


def test_read_recent_limit(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    entry = _entry()
    for i in range(20):
        store.append(entry, _history(bug_id=f"B-{i}"))
    recent = store.read_recent(entry, limit=3)
    assert len(recent) == 3
    assert recent[0].bug_id == "B-17"
    assert recent[2].bug_id == "B-19"


def test_read_empty_returns_empty(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    assert store.read_recent(_entry()) == []


def test_to_prompt_block_empty() -> None:
    store = HistoryStore(Path("/tmp"))
    assert store.to_prompt_block([]) == ""


def test_to_prompt_block_renders() -> None:
    store = HistoryStore(Path("/tmp"))
    block = store.to_prompt_block([_history()])
    assert "Previously fixed" in block
    assert "B-1" in block
    assert "src/App.jsx" in block
    assert "end history" in block


def test_path_for_uses_slug(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    path = store.path_for(_entry())
    assert path.name == "acme__widgets.history.jsonl"


def test_corrupt_line_skipped(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    entry = _entry()
    store.append(entry, _history(bug_id="B-good"))
    # Inject a corrupt line
    path = store.path_for(entry)
    with path.open("a", encoding="utf-8") as f:
        f.write("not valid json\n")
    store.append(entry, _history(bug_id="B-also-good"))
    recent = store.read_recent(entry)
    assert len(recent) == 2
    assert recent[0].bug_id == "B-good"
    assert recent[1].bug_id == "B-also-good"
