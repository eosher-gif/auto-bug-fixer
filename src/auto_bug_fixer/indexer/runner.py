"""Orchestrates indexing of every repo in the registry."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from auto_bug_fixer.git_ops.repo import GitClient, GitOperationError
from auto_bug_fixer.indexer.index_store import IndexStore
from auto_bug_fixer.indexer.repo_index import RepoIndexBuilder
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.registry import RegistryEntry, RepoRegistry

log = get_logger(__name__)


class IndexRunner:
    """Clones each registry entry, builds a ``RepoIndex``, and persists it."""

    def __init__(
        self,
        *,
        registry: RepoRegistry,
        store: IndexStore,
        git: GitClient,
        builder: RepoIndexBuilder | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        """Bind the runner to its collaborators."""
        self._registry = registry
        self._store = store
        self._git = git
        self._builder = builder or RepoIndexBuilder()
        self._scratch_dir = scratch_dir

    def index_all(self) -> int:
        """Index every repository in the registry. Returns the success count."""
        successes = 0
        for entry in self._registry:
            try:
                self.index_one(entry)
                successes += 1
            except (GitOperationError, OSError) as exc:
                log.warning("indexing_failed", url=entry.url, error=str(exc))
        log.info("indexing_run_complete", attempted=len(self._registry), successes=successes)
        return successes

    def index_one(self, entry: RegistryEntry) -> Path:
        """Clone, build, and persist the index for a single ``entry``."""
        scratch_root = self._scratch_dir or Path(tempfile.gettempdir())
        scratch_root.mkdir(parents=True, exist_ok=True)
        clone_dir = Path(tempfile.mkdtemp(prefix=f"index-{entry.slug}-", dir=scratch_root))
        try:
            self._git.clone(entry.url, entry.default_branch, clone_dir)
            index = self._builder.build(
                url=entry.url,
                default_branch=entry.default_branch,
                repo_root=clone_dir,
                description=entry.description,
                explicit_language=entry.language,
                explicit_test_command=entry.test_command,
            )
            return self._store.save(entry, index)
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)
