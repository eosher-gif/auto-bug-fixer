"""Domain models shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bug:
    """A bug record loaded from the customer database.

    `repo_url` and `base_branch` are derived in the repository layer from a
    project-name field on the source ticket (e.g. Firestore "project") via a
    project resolver, so the rest of the pipeline does not need to know
    where the ticket came from.
    """

    id: str
    title: str
    description: str
    repo_url: str
    base_branch: str
    reporter_email: str | None
    ticket_type: str = "bug"
    customer_name: str | None = None
    project_name: str | None = None
    image_urls: tuple[str, ...] = ()
    source_branch: str | None = None
    source_pr_url: str | None = None


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
    preview_url: str | None = None
    commit_sha: str | None = None
