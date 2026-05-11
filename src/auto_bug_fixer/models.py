"""Domain models shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bug:
    """A bug record loaded from the customer database."""

    id: str
    title: str
    description: str
    repo_url: str
    base_branch: str
    reporter_email: str | None


@dataclass
class FixOutcome:
    """Result of asking Claude to fix a bug."""

    success: bool
    summary: str
    changed_files: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class PullRequest:
    """A GitHub pull request opened for a bug fix."""

    number: int
    url: str
    branch: str
    title: str
