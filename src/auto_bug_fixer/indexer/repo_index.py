"""Build a structured ``RepoIndex`` for a single repository.

The index is intentionally compact (a few KB) and deterministic so it can be
injected into Claude's context window cheaply on every bug.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)

MAX_TREE_ENTRIES = 400
MAX_TREE_DEPTH = 4
MAX_README_BYTES = 6_000
KEY_FILES = (
    "README.md",
    "README.rst",
    "README",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "ARCHITECTURE.md",
    "Makefile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "tsconfig.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Dockerfile",
    "docker-compose.yml",
    ".github/workflows",
)
LANGUAGE_HINTS: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
    "composer.json": "php",
}
TEST_COMMAND_HINTS: dict[str, str] = {
    "pytest.ini": "pytest -q",
    "pyproject.toml": "pytest -q",
    "tox.ini": "pytest -q",
    "package.json": "npm test --silent",
    "go.mod": "go test ./...",
    "Cargo.toml": "cargo test --quiet",
}
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "target",
    ".tox",
}


@dataclass
class RepoIndex:
    """Snapshot of a repository structure used as Claude context."""

    url: str
    default_branch: str
    indexed_at: str
    detected_language: str | None
    suggested_test_command: str | None
    description: str | None
    readme_excerpt: str
    tree: list[str] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RepoIndex:
        """Build an index from a previously serialized dict."""
        return cls(**data)

    def to_prompt_block(self) -> str:
        """Render a compact textual block for inclusion in Claude's prompt."""
        tree_block = "\n".join(self.tree) if self.tree else "(tree omitted)"
        key_block = "\n".join(f"- {p}" for p in self.key_files) or "(none)"
        readme_block = self.readme_excerpt or "(no README)"
        return (
            f"Repository: {self.url} (branch: {self.default_branch})\n"
            f"Indexed at (UTC): {self.indexed_at}\n"
            f"Detected language: {self.detected_language or 'unknown'}\n"
            f"Suggested test command: {self.suggested_test_command or 'unknown'}\n"
            f"Description:\n{self.description or '(none)'}\n\n"
            f"Key files present:\n{key_block}\n\n"
            f"Top-of-tree (max {MAX_TREE_DEPTH} levels, "
            f"{MAX_TREE_ENTRIES} entries):\n{tree_block}\n\n"
            f"README excerpt (first {MAX_README_BYTES} bytes):\n{readme_block}\n"
        )


class RepoIndexBuilder:
    """Walks a cloned repo and produces a compact ``RepoIndex``."""

    def build(
        self,
        *,
        url: str,
        default_branch: str,
        repo_root: Path,
        description: str | None,
        explicit_language: str | None,
        explicit_test_command: str | None,
    ) -> RepoIndex:
        """Build the index for the repo cloned at ``repo_root``."""
        repo_root = repo_root.resolve()
        if not repo_root.is_dir():
            raise FileNotFoundError(f"repo_root does not exist: {repo_root}")

        present_files = _collect_top_level_marker_files(repo_root)
        language = explicit_language or _infer_language(present_files)
        test_command = explicit_test_command or _infer_test_command(present_files)
        readme = _read_readme(repo_root)
        tree = _walk_tree(repo_root)

        index = RepoIndex(
            url=url,
            default_branch=default_branch,
            indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            detected_language=language,
            suggested_test_command=test_command,
            description=description,
            readme_excerpt=readme,
            tree=tree,
            key_files=sorted(present_files),
        )
        log.info(
            "repo_indexed",
            url=url,
            language=language,
            files=len(present_files),
            tree_entries=len(tree),
        )
        return index


def _collect_top_level_marker_files(repo_root: Path) -> list[str]:
    found: list[str] = []
    for marker in KEY_FILES:
        target = repo_root / marker
        if target.exists():
            found.append(marker)
    return found


def _infer_language(present_files: list[str]) -> str | None:
    for marker in present_files:
        if marker in LANGUAGE_HINTS:
            return LANGUAGE_HINTS[marker]
    return None


def _infer_test_command(present_files: list[str]) -> str | None:
    for marker in present_files:
        if marker in TEST_COMMAND_HINTS:
            return TEST_COMMAND_HINTS[marker]
    return None


def _read_readme(repo_root: Path) -> str:
    for candidate in ("README.md", "README.rst", "README"):
        target = repo_root / candidate
        if not target.is_file():
            continue
        try:
            data = target.read_bytes()[:MAX_README_BYTES]
            return data.decode("utf-8", errors="replace")
        except OSError:
            continue
    return ""


def _walk_tree(repo_root: Path) -> list[str]:
    """Return a depth-limited, count-limited POSIX-path listing of the repo."""
    entries: list[str] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > MAX_TREE_DEPTH or len(entries) >= MAX_TREE_ENTRIES:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children:
            if len(entries) >= MAX_TREE_ENTRIES:
                entries.append("... (tree truncated)")
                return
            if child.name in SKIP_DIRS:
                continue
            rel = child.relative_to(repo_root).as_posix()
            if child.is_dir():
                entries.append(f"{rel}/")
                _walk(child, depth + 1)
            else:
                entries.append(rel)

    _walk(repo_root, depth=1)
    return entries


def write_index(index: RepoIndex, dest: Path) -> None:
    """Persist ``index`` to ``dest`` as JSON, atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(index.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(dest)


def read_index(path: Path) -> RepoIndex:
    """Load an index from disk. Raises ValueError on corrupt JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt index file: {path}") from exc
    return RepoIndex.from_dict(data)
