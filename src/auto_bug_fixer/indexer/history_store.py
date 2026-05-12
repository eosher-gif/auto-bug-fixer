"""Persistent per-repo history of past fixes.

Each successful fix is appended as a JSONL line alongside the repo index.
When the next bug arrives for the same repo, recent history entries are
injected into Claude's prompt so it can learn from past patterns.

Privacy: only titles, file paths, and PR URLs are stored (no PII or
full ticket descriptions) because the auto-bug-fixer repo may be public.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.registry import RegistryEntry

log = get_logger(__name__)


@dataclass(frozen=True)
class HistoryEntry:
    """One successful fix recorded in the per-repo ledger."""

    bug_id: str
    ticket_title: str
    pr_url: str
    pr_number: int
    files_touched: list[str]
    ai_summary: str
    ts: str  # ISO timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> HistoryEntry:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class HistoryStore:
    """Append-only JSONL ledger per repository, stored alongside indexes."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir

    def path_for(self, entry: RegistryEntry) -> Path:
        """Return the JSONL path for a registry entry."""
        return self._base / f"{entry.slug}.history.jsonl"

    def append(self, entry: RegistryEntry, history: HistoryEntry) -> None:
        """Atomically append one entry to the ledger."""
        path = self.path_for(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(history.to_dict(), ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
        log.info(
            "history_appended",
            repo=entry.slug,
            bug_id=history.bug_id,
            files=len(history.files_touched),
        )

    def read_recent(self, entry: RegistryEntry, limit: int = 10) -> list[HistoryEntry]:
        """Read the last ``limit`` entries for a repo."""
        path = self.path_for(entry)
        if not path.is_file():
            return []
        lines: list[str] = []
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            return []
        entries: list[HistoryEntry] = []
        for line in lines[-limit:]:
            try:
                entries.append(HistoryEntry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return entries

    def to_prompt_block(self, entries: list[HistoryEntry]) -> str:
        """Render history entries as a prompt section for Claude."""
        if not entries:
            return ""
        lines = [
            "--- Previously fixed tickets in this repo "
            "(use as guidance only, do not blindly copy) ---"
        ]
        for h in entries:
            files = ", ".join(h.files_touched[:5])
            if len(h.files_touched) > 5:
                files += f" (+{len(h.files_touched) - 5} more)"
            lines.append(
                f"- Bug {h.bug_id}: {h.ticket_title}\n"
                f"  Files: {files}\n"
                f"  Summary: {h.ai_summary}\n"
                f"  PR: {h.pr_url}"
            )
        lines.append("--- end history ---")
        return "\n".join(lines)
