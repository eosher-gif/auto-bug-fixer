"""On-disk store for ``RepoIndex`` JSON files."""
from __future__ import annotations

from pathlib import Path

from auto_bug_fixer.indexer.repo_index import RepoIndex, read_index, write_index
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.registry import RegistryEntry

log = get_logger(__name__)


class IndexStore:
    """Maps a registry entry slug to a JSON index file in a base directory."""

    def __init__(self, base_dir: Path) -> None:
        """Bind the store to a base directory (created on demand)."""
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)

    def path_for(self, entry: RegistryEntry) -> Path:
        """Return the JSON file path for ``entry``."""
        return self._base / f"{entry.slug}.json"

    def save(self, entry: RegistryEntry, index: RepoIndex) -> Path:
        """Persist ``index`` for ``entry`` and return the path written."""
        target = self.path_for(entry)
        write_index(index, target)
        log.info("index_saved", slug=entry.slug, path=str(target))
        return target

    def load(self, entry: RegistryEntry) -> RepoIndex | None:
        """Return the previously saved index, or ``None`` if not on disk."""
        path = self.path_for(entry)
        if not path.exists():
            return None
        try:
            return read_index(path)
        except ValueError as exc:
            log.warning("index_corrupt", slug=entry.slug, error=str(exc))
            return None

    def load_by_url(
        self,
        url: str,
        registry_lookup,
    ) -> RepoIndex | None:
        """Resolve ``url`` via ``registry_lookup`` and load the matching index."""
        entry = registry_lookup(url)
        if entry is None:
            return None
        return self.load(entry)
