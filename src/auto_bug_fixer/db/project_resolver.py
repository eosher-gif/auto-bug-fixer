"""Resolve a free-text project name on a ticket to a registered repo.

Tickets in the source database (Firestore) carry a free-text `project` field
written by humans, often in Hebrew, e.g. ``"ארגמן"`` / ``"Argaman"`` /
``"  ARGAMAN  "``. The resolver normalizes the input and looks it up against
the ``display_names`` list of every entry in ``repos.yaml``.

The resolver is intentionally tiny — no fuzzy matching, no third-party deps.
A ticket with an unknown project gets rejected at intake so the pipeline
never tries to guess.
"""
from __future__ import annotations

from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.registry import RegistryEntry, RepoRegistry

log = get_logger(__name__)


class UnknownProjectError(LookupError):
    """Raised when a ticket's project name does not match any registered repo."""


class ProjectResolver:
    """Map a free-text project name to a `RegistryEntry`.

    Wraps a `RepoRegistry` and exposes a single `resolve()` method so the
    repository layer never touches the registry directly. Easy to mock in
    tests.
    """

    def __init__(self, registry: RepoRegistry) -> None:
        """Bind the resolver to the loaded repo registry."""
        self._registry = registry

    def resolve(self, project_name: str | None) -> RegistryEntry:
        """Return the entry whose `display_names` matches `project_name`.

        Raises:
            UnknownProjectError: when `project_name` is empty or no entry
                claims that display name.
        """
        if not project_name or not project_name.strip():
            raise UnknownProjectError("ticket has empty project field")
        entry = self._registry.by_display_name(project_name)
        if entry is None:
            log.warning("project_unresolved", project_name=project_name)
            raise UnknownProjectError(
                f"no registered repo claims display name {project_name!r}"
            )
        return entry
