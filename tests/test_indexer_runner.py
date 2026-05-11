"""Tests for IndexRunner orchestration using a fake GitClient (no network)."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from auto_bug_fixer.indexer.index_store import IndexStore
from auto_bug_fixer.indexer.runner import IndexRunner
from auto_bug_fixer.registry import RegistryEntry, RepoRegistry


@dataclass
class _CopyingGitClient:
    """Fake GitClient that 'clones' by copying a fixed source tree."""

    source: Path
    clones: int = 0

    def clone(self, repo_url: str, branch: str, dest: Path) -> None:
        self.clones += 1
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.source, dest)


def _entry(url: str = "https://github.com/acme/widgets") -> RegistryEntry:
    return RegistryEntry(
        url=url,
        default_branch="main",
        language=None,
        test_command=None,
        description="local test repo",
    )


def test_runner_indexes_every_entry(tmp_path: Path, fake_repo_tree: Path) -> None:
    registry = RepoRegistry(
        entries=(
            _entry("https://github.com/acme/widgets"),
            _entry("https://github.com/acme/api"),
        )
    )
    store = IndexStore(base_dir=tmp_path / "indices")
    git = _CopyingGitClient(source=fake_repo_tree)
    runner = IndexRunner(
        registry=registry,
        store=store,
        git=git,  # type: ignore[arg-type]
        scratch_dir=tmp_path / "scratch",
    )
    successes = runner.index_all()
    assert successes == 2
    assert git.clones == 2
    for entry in registry:
        loaded = store.load(entry)
        assert loaded is not None
        assert loaded.detected_language == "python"
        assert "README.md" in loaded.key_files


def test_runner_continues_after_clone_failure(
    tmp_path: Path, fake_repo_tree: Path
) -> None:
    registry = RepoRegistry(
        entries=(
            _entry("https://github.com/acme/widgets"),
            _entry("https://github.com/acme/api"),
        )
    )
    store = IndexStore(base_dir=tmp_path / "indices")

    @dataclass
    class _FlakyGit:
        source: Path
        attempt: int = 0

        def clone(self, repo_url: str, branch: str, dest: Path) -> None:
            self.attempt += 1
            if "widgets" in repo_url:
                from auto_bug_fixer.git_ops.repo import GitOperationError

                raise GitOperationError("simulated failure")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(self.source, dest)

    runner = IndexRunner(
        registry=registry,
        store=store,
        git=_FlakyGit(source=fake_repo_tree),  # type: ignore[arg-type]
        scratch_dir=tmp_path / "scratch",
    )
    successes = runner.index_all()
    assert successes == 1
    assert store.load(registry.entries[0]) is None
    assert store.load(registry.entries[1]) is not None


def test_runner_cleans_up_scratch_dir(tmp_path: Path, fake_repo_tree: Path) -> None:
    registry = RepoRegistry(entries=(_entry(),))
    store = IndexStore(base_dir=tmp_path / "indices")
    scratch = tmp_path / "scratch"
    runner = IndexRunner(
        registry=registry,
        store=store,
        git=_CopyingGitClient(source=fake_repo_tree),  # type: ignore[arg-type]
        scratch_dir=scratch,
    )
    runner.index_all()
    assert not list(scratch.glob("index-*"))
