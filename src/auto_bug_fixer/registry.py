"""Loader and validator for the ``repos.yaml`` repository registry."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from auto_bug_fixer.git_ops.repo import (
    GitOperationError,
    RepoCoordinates,
    parse_github_url,
)
from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)


class RegistryError(ValueError):
    """Raised when ``repos.yaml`` is missing, malformed, or invalid."""


@dataclass(frozen=True)
class RegistryEntry:
    """One repository the bug-fixer is responsible for."""

    url: str
    default_branch: str
    language: str | None
    test_command: str | None
    description: str | None

    @property
    def coords(self) -> RepoCoordinates:
        """Return the GitHub owner/name pair parsed from ``url``."""
        return parse_github_url(self.url)

    @property
    def slug(self) -> str:
        """Return ``owner__name`` — safe to use as a filename."""
        c = self.coords
        return f"{c.owner}__{c.name}"


@dataclass(frozen=True)
class RepoRegistry:
    """The full collection of repositories from ``repos.yaml``."""

    entries: tuple[RegistryEntry, ...]

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def by_url(self, url: str) -> RegistryEntry | None:
        """Return the entry whose URL matches ``url`` (case-insensitive), or None."""
        normalized = url.strip().lower().rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        for entry in self.entries:
            candidate = entry.url.strip().lower().rstrip("/")
            if candidate.endswith(".git"):
                candidate = candidate[:-4]
            if candidate == normalized:
                return entry
        return None


def load_registry(path: Path) -> RepoRegistry:
    """Load and validate a ``repos.yaml`` file.

    Args:
        path: Filesystem path to the registry file.

    Raises:
        RegistryError: when the file is missing, malformed, or contains invalid
            entries.
    """
    if not path.exists():
        raise RegistryError(f"registry file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict) or "repos" not in raw:
        raise RegistryError("registry must be a mapping with top-level 'repos' key")

    items = raw.get("repos") or []
    if not isinstance(items, list):
        raise RegistryError("'repos' must be a list")
    if not items:
        raise RegistryError("'repos' must contain at least one entry")

    entries: list[RegistryEntry] = []
    seen_urls: set[str] = set()
    for index, item in enumerate(items):
        entries.append(_parse_entry(index, item, seen_urls))

    log.info("registry_loaded", count=len(entries), path=str(path))
    return RepoRegistry(entries=tuple(entries))


def _parse_entry(index: int, item: object, seen_urls: set[str]) -> RegistryEntry:
    if not isinstance(item, dict):
        raise RegistryError(f"repos[{index}] must be a mapping, got {type(item).__name__}")

    url = item.get("url")
    branch = item.get("default_branch")
    if not isinstance(url, str) or not url.strip():
        raise RegistryError(f"repos[{index}].url is required and must be a string")
    if not isinstance(branch, str) or not branch.strip():
        raise RegistryError(
            f"repos[{index}].default_branch is required and must be a string"
        )
    try:
        parse_github_url(url)
    except GitOperationError as exc:
        raise RegistryError(f"repos[{index}].url invalid: {exc}") from exc
    if url in seen_urls:
        raise RegistryError(f"repos[{index}].url duplicates an earlier entry: {url}")
    seen_urls.add(url)

    return RegistryEntry(
        url=url.strip(),
        default_branch=branch.strip(),
        language=_optional_str(item.get("language")),
        test_command=_optional_str(item.get("test_command")),
        description=_optional_str(item.get("description")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryError(f"expected string, got {type(value).__name__}: {value!r}")
    text = value.strip()
    return text or None
