"""Tests for ProjectResolver — the Hebrew project-name -> repo lookup."""
from __future__ import annotations

import pytest

from auto_bug_fixer.db.project_resolver import ProjectResolver, UnknownProjectError
from auto_bug_fixer.registry import RegistryEntry, RepoRegistry

_ARGAMAN = RegistryEntry(
    url="https://github.com/talya-debug/argaman-new",
    default_branch="master",
    language="javascript",
    test_command=None,
    description=None,
    framework="react",
    forbidden_paths=(),
    display_names=("ארגמן", "Argaman"),
)
_LIA = RegistryEntry(
    url="https://github.com/talya-debug/lia-fine-jewelry",
    default_branch="master",
    language="javascript",
    test_command=None,
    description=None,
    framework="react",
    forbidden_paths=(),
    display_names=("ליה", "LIA"),
)


def _resolver() -> ProjectResolver:
    return ProjectResolver(RepoRegistry(entries=(_ARGAMAN, _LIA)))


def test_resolves_exact_hebrew_match() -> None:
    assert _resolver().resolve("ארגמן") is _ARGAMAN


def test_resolves_latin_synonym() -> None:
    assert _resolver().resolve("Argaman") is _ARGAMAN


def test_resolution_is_case_insensitive() -> None:
    assert _resolver().resolve("argaman") is _ARGAMAN
    assert _resolver().resolve("ARGAMAN") is _ARGAMAN


def test_resolution_collapses_whitespace() -> None:
    """Stray surrounding whitespace and tabs collapse the same way."""
    assert _resolver().resolve("  Argaman  ") is _ARGAMAN
    assert _resolver().resolve("\tArgaman\n") is _ARGAMAN
    assert _resolver().resolve("  ארגמן  ") is _ARGAMAN


def test_disambiguates_between_two_repos() -> None:
    r = _resolver()
    assert r.resolve("ליה") is _LIA
    assert r.resolve("ארגמן") is _ARGAMAN


def test_unknown_project_raises() -> None:
    with pytest.raises(UnknownProjectError, match="no registered repo"):
        _resolver().resolve("פרויקט שלא קיים")


def test_empty_input_raises() -> None:
    with pytest.raises(UnknownProjectError, match="empty project field"):
        _resolver().resolve("")
    with pytest.raises(UnknownProjectError):
        _resolver().resolve("   ")
    with pytest.raises(UnknownProjectError):
        _resolver().resolve(None)


def test_entry_with_no_display_names_is_unreachable_by_name() -> None:
    """A misconfigured entry without `display_names` should never match."""
    nameless = RegistryEntry(
        url="https://github.com/x/y",
        default_branch="main",
        language=None,
        test_command=None,
        description=None,
    )
    resolver = ProjectResolver(RepoRegistry(entries=(nameless,)))
    with pytest.raises(UnknownProjectError):
        resolver.resolve("anything")
