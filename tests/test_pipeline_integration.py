"""End-to-end pipeline test using in-memory fakes for every collaborator.

Verifies that the orchestration is correct without touching the real DB,
Claude API, GitHub, SMTP, or git CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from auto_bug_fixer.config import Settings
from auto_bug_fixer.git_ops.repo import RepoCoordinates
from auto_bug_fixer.indexer.index_store import IndexStore
from auto_bug_fixer.indexer.repo_index import RepoIndex
from auto_bug_fixer.models import Bug, FixOutcome, PullRequest
from auto_bug_fixer.pipeline import BugFixPipeline
from auto_bug_fixer.registry import RegistryEntry, RepoRegistry


def _settings(workspace: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_api_key="x",
        database_url="sqlite:///:memory:",
        github_token="tkn",
        smtp_host="smtp.example",
        smtp_username="u",
        smtp_password="p",
        notify_from="[email protected]",
        workspace_dir=workspace,
        bug_status_new="new",
        bug_status_processing="processing",
        bug_status_mr_opened="mr_opened",
        bug_status_failed="failed",
    )


@dataclass
class FakeBugRepository:
    bugs: list[Bug]
    statuses: dict[str, str] = field(default_factory=dict)

    def fetch_pending(self, limit: int) -> list[Bug]:
        return self.bugs[:limit]

    def mark_status(self, bug_id: str, new_status: str) -> None:
        self.statuses.setdefault(bug_id, "")
        self.statuses[bug_id] = new_status


@dataclass
class FakeAgent:
    outcome: FixOutcome
    seen_indexes: list[RepoIndex | None] = field(default_factory=list)
    seen_bugs: list[Bug] = field(default_factory=list)

    def fix_bug(
        self,
        bug: Bug,
        repo_root: Path,
        repo_index: RepoIndex | None = None,
    ) -> FixOutcome:
        self.seen_bugs.append(bug)
        self.seen_indexes.append(repo_index)
        if self.outcome.changed_files:
            for rel in self.outcome.changed_files:
                target = repo_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("patched\n", encoding="utf-8")
        return self.outcome


@dataclass
class FakeGit:
    cloned: list[tuple[str, str, Path]] = field(default_factory=list)
    branches: list[tuple[Path, str]] = field(default_factory=list)
    commits: list[tuple[Path, str]] = field(default_factory=list)
    pushes: list[tuple[Path, str, str]] = field(default_factory=list)
    has_changes: bool = True

    def clone(self, repo_url: str, branch: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README.md").write_text("readme", encoding="utf-8")
        self.cloned.append((repo_url, branch, dest))

    def create_branch(self, repo_dir: Path, branch_name: str) -> None:
        self.branches.append((repo_dir, branch_name))

    def commit_all(self, repo_dir: Path, message: str) -> bool:
        self.commits.append((repo_dir, message))
        return self.has_changes

    def push(self, repo_dir: Path, branch_name: str, remote_url: str) -> None:
        self.pushes.append((repo_dir, branch_name, remote_url))


@dataclass
class FakeGitHub:
    next_pr_number: int = 100
    calls: list[dict] = field(default_factory=list)

    def open_pull_request(
        self,
        coords: RepoCoordinates,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequest:
        self.calls.append(
            {
                "coords": coords,
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            }
        )
        number = self.next_pr_number
        self.next_pr_number += 1
        return PullRequest(
            number=number,
            url=f"https://github.com/{coords.owner}/{coords.name}/pull/{number}",
            branch=head_branch,
            title=title,
        )


@dataclass
class FakeEmail:
    successes: list[tuple[Bug, PullRequest]] = field(default_factory=list)
    failures: list[tuple[Bug, FixOutcome]] = field(default_factory=list)

    def notify_success(self, bug, outcome, pr):
        self.successes.append((bug, pr))

    def notify_failure(self, bug, outcome):
        self.failures.append((bug, outcome))


def _bug() -> Bug:
    return Bug(
        id="B-42",
        title="title",
        description="desc",
        repo_url="https://github.com/acme/widgets",
        base_branch="main",
        reporter_email="[email protected]",
    )


def _registry_with_index(tmp_path: Path) -> tuple[RepoRegistry, IndexStore, RepoIndex]:
    entry = RegistryEntry(
        url="https://github.com/acme/widgets",
        default_branch="main",
        language="python",
        test_command="pytest",
        description=None,
    )
    registry = RepoRegistry(entries=(entry,))
    store = IndexStore(base_dir=tmp_path / "indices")
    index = RepoIndex(
        url=entry.url,
        default_branch="main",
        indexed_at="2026-01-01T00:00:00+00:00",
        detected_language="python",
        suggested_test_command="pytest",
        description=None,
        readme_excerpt="readme",
        tree=["src/"],
        key_files=["pyproject.toml"],
    )
    store.save(entry, index)
    return registry, store, index


def _build_pipeline(
    tmp_path: Path,
    *,
    bug_repo: FakeBugRepository,
    agent: FakeAgent,
    git: FakeGit,
    github: FakeGitHub,
    email: FakeEmail,
    registry: RepoRegistry | None = None,
    index_store: IndexStore | None = None,
) -> BugFixPipeline:
    return BugFixPipeline(
        _settings(tmp_path / "ws"),
        bug_repository=bug_repo,  # type: ignore[arg-type]
        agent=agent,  # type: ignore[arg-type]
        git=git,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        email=email,  # type: ignore[arg-type]
        registry=registry,
        index_store=index_store,
    )


def test_happy_path_opens_pr_and_emails(tmp_path: Path) -> None:
    bug = _bug()
    bug_repo = FakeBugRepository(bugs=[bug])
    agent = FakeAgent(
        outcome=FixOutcome(success=True, summary="fixed it", changed_files=["src/x.py"])
    )
    git = FakeGit()
    github = FakeGitHub()
    email = FakeEmail()
    registry, store, index = _registry_with_index(tmp_path)

    pipeline = _build_pipeline(
        tmp_path,
        bug_repo=bug_repo,
        agent=agent,
        git=git,
        github=github,
        email=email,
        registry=registry,
        index_store=store,
    )
    handled = pipeline.run_once()
    assert handled == 1
    assert bug_repo.statuses["B-42"] == "mr_opened"
    assert len(github.calls) == 1
    assert github.calls[0]["base"] == "main"
    assert github.calls[0]["head"].startswith("auto-bug-fixer/bug-B-42-")
    assert len(email.successes) == 1
    assert email.successes[0][1].number == 100
    assert agent.seen_indexes == [index]


def test_no_repo_index_when_registry_missing(tmp_path: Path) -> None:
    bug_repo = FakeBugRepository(bugs=[_bug()])
    agent = FakeAgent(
        outcome=FixOutcome(success=True, summary="fixed", changed_files=["x.py"])
    )
    pipeline = _build_pipeline(
        tmp_path,
        bug_repo=bug_repo,
        agent=agent,
        git=FakeGit(),
        github=FakeGitHub(),
        email=FakeEmail(),
        registry=None,
        index_store=None,
    )
    pipeline.run_once()
    assert agent.seen_indexes == [None]


def test_failure_outcome_marks_failed_and_emails(tmp_path: Path) -> None:
    bug_repo = FakeBugRepository(bugs=[_bug()])
    agent = FakeAgent(
        outcome=FixOutcome(success=False, summary="cannot fix", error="root")
    )
    email = FakeEmail()
    github = FakeGitHub()
    pipeline = _build_pipeline(
        tmp_path,
        bug_repo=bug_repo,
        agent=agent,
        git=FakeGit(),
        github=github,
        email=email,
    )
    pipeline.run_once()
    assert bug_repo.statuses["B-42"] == "failed"
    assert github.calls == []
    assert len(email.failures) == 1
    assert email.successes == []


def test_no_changes_after_finish_marks_failed(tmp_path: Path) -> None:
    bug_repo = FakeBugRepository(bugs=[_bug()])
    agent = FakeAgent(
        outcome=FixOutcome(success=True, summary="claims fixed", changed_files=["x.py"])
    )
    git = FakeGit(has_changes=False)
    email = FakeEmail()
    pipeline = _build_pipeline(
        tmp_path,
        bug_repo=bug_repo,
        agent=agent,
        git=git,
        github=FakeGitHub(),
        email=email,
    )
    pipeline.run_once()
    assert bug_repo.statuses["B-42"] == "failed"
    assert len(email.failures) == 1


def test_empty_db_returns_zero(tmp_path: Path) -> None:
    pipeline = _build_pipeline(
        tmp_path,
        bug_repo=FakeBugRepository(bugs=[]),
        agent=FakeAgent(outcome=FixOutcome(success=True, summary="x")),
        git=FakeGit(),
        github=FakeGitHub(),
        email=FakeEmail(),
    )
    assert pipeline.run_once() == 0


def test_processes_at_most_max_bugs_per_run(tmp_path: Path) -> None:
    bugs = [
        Bug(id=f"B{i}", title="t", description="d",
            repo_url="https://github.com/a/b", base_branch="main",
            reporter_email=None)
        for i in range(10)
    ]
    bug_repo = FakeBugRepository(bugs=bugs)
    pipeline = _build_pipeline(
        tmp_path,
        bug_repo=bug_repo,
        agent=FakeAgent(
            outcome=FixOutcome(success=True, summary="ok", changed_files=["x.py"])
        ),
        git=FakeGit(),
        github=FakeGitHub(),
        email=FakeEmail(),
    )
    handled = pipeline.run_once()
    assert handled == 3
    assert sorted(bug_repo.statuses.keys()) == ["B0", "B1", "B2"]
