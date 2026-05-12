"""Build a structured ``RepoIndex`` for a single repository.

The index is intentionally compact (a few KB) and deterministic so it can be
injected into Claude's context window cheaply on every bug.

Deep-index additions (v2): extracts dependencies, routes, component map,
and key file snippets so Claude starts with real understanding of the
codebase rather than just a directory listing.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)

MAX_TREE_ENTRIES = 400
MAX_TREE_DEPTH = 4
MAX_README_BYTES = 6_000
MAX_SNIPPET_BYTES = 1_500
MAX_SNIPPETS = 3
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
    # Deep-index v2 fields
    dependencies: dict[str, str] = field(default_factory=dict)
    routes: list[str] = field(default_factory=list)
    component_map: dict[str, list[str]] = field(default_factory=dict)
    key_snippets: dict[str, str] = field(default_factory=dict)
    entry_points: list[str] = field(default_factory=list)
    framework_details: str | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RepoIndex:
        """Build an index from a previously serialized dict."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def to_prompt_block(self) -> str:
        """Render a MINIMAL context block to save tokens.

        Keep it under ~1500 chars. Claude can use tools to read more.
        """
        # Only pages/routes — most useful for navigation bugs
        routes_block = ""
        if self.routes:
            routes_block = "\nPages: " + ", ".join(
                r.rsplit("/", 1)[-1].replace(".jsx", "").replace(".tsx", "")
                for r in self.routes[:15]
            )

        framework = f"\nStack: {self.framework_details}" if self.framework_details else ""
        entry = f"\nEntry: {', '.join(self.entry_points)}" if self.entry_points else ""

        return (
            f"Repo: {self.url} (branch: {self.default_branch})\n"
            f"Language: {self.detected_language or '?'}"
            f"{framework}{entry}{routes_block}\n"
            f"Use list_dir and read_file to explore. Fix fast.\n"
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

        # Deep-index v2: extract richer context
        dependencies = _extract_dependencies(repo_root, language)
        routes = _extract_routes(repo_root, language)
        component_map = _extract_component_map(repo_root, language)
        key_snippets = _extract_key_snippets(repo_root, language)
        entry_points = _detect_entry_points(repo_root, language)
        framework_details = _detect_framework_details(repo_root, language)

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
            dependencies=dependencies,
            routes=routes,
            component_map=component_map,
            key_snippets=key_snippets,
            entry_points=entry_points,
            framework_details=framework_details,
        )
        log.info(
            "repo_indexed",
            url=url,
            language=language,
            files=len(present_files),
            tree_entries=len(tree),
            deps=len(dependencies),
            routes=len(routes),
            components=len(component_map),
            snippets=len(key_snippets),
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


def _safe_read(path: Path, max_bytes: int = MAX_SNIPPET_BYTES) -> str:
    """Read a file up to ``max_bytes``, returning empty on any error."""
    try:
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _extract_dependencies(repo_root: Path, language: str | None) -> dict[str, str]:
    """Extract top-level dependencies from package manifest."""
    deps: dict[str, str] = {}
    pkg_json = repo_root / "package.json"
    if pkg_json.is_file():
        try:
            raw = json.loads(pkg_json.read_text(encoding="utf-8"))
            for key in ("dependencies",):  # skip devDependencies to save tokens
                section = raw.get(key, {})
                if isinstance(section, dict):
                    for name, ver in section.items():
                        deps[name] = str(ver)
        except (json.JSONDecodeError, OSError):
            pass
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file() and language in ("python", None):
        content = _safe_read(pyproject)
        in_deps = False
        for line in content.splitlines():
            if re.match(r"^\[.*dependencies.*\]", line, re.IGNORECASE):
                in_deps = True
                continue
            if line.startswith("["):
                in_deps = False
                continue
            if in_deps:
                m = re.match(r'^"?([a-zA-Z0-9_-]+)"?\s*[>=<~^]', line)
                if m:
                    deps[m.group(1)] = line.strip()
    req_txt = repo_root / "requirements.txt"
    if req_txt.is_file() and language in ("python", None):
        for line in _safe_read(req_txt).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                name = re.split(r"[>=<~!\[]", line)[0].strip()
                if name:
                    deps[name] = line
    return deps


def _extract_routes(repo_root: Path, language: str | None) -> list[str]:
    """Extract route/page definitions from React/JS/TS projects."""
    routes: list[str] = []
    if language not in ("javascript", "typescript", None):
        return routes
    # Scan for React Router patterns or page configs
    for pattern_dir in ("src/pages", "src/views", "pages", "app"):
        pages_dir = repo_root / pattern_dir
        if pages_dir.is_dir():
            for f in sorted(pages_dir.iterdir()):
                if f.is_file() and f.suffix in (".jsx", ".tsx", ".js", ".ts"):
                    routes.append(f"{pattern_dir}/{f.name}")
    # Also scan for route definitions in source files
    route_patterns = [
        re.compile(r'path:\s*["\']([^"\']+)["\']'),
        re.compile(r'<Route\s+path=["\']([^"\']+)["\']'),
    ]
    # Check pages.config.js or similar
    for config_name in ("src/pages.config.js", "src/routes.js", "src/routes.tsx",
                        "src/App.jsx", "src/App.tsx", "src/App.js"):
        config_path = repo_root / config_name
        if config_path.is_file():
            content = _safe_read(config_path)
            for rp in route_patterns:
                for match in rp.finditer(content):
                    route = match.group(1)
                    if route not in routes:
                        routes.append(route)
    return routes


def _extract_component_map(
    repo_root: Path, language: str | None
) -> dict[str, list[str]]:
    """Build a lightweight import map for key components (pages/features)."""
    cmap: dict[str, list[str]] = {}
    if language not in ("javascript", "typescript", None):
        return cmap
    import_re = re.compile(
        r"import\s+.*?from\s+['\"]([^'\"]+)['\"]", re.MULTILINE
    )
    components_dir = repo_root / "src" / "components"
    pages_dir = repo_root / "src" / "pages"
    for scan_dir in (pages_dir, components_dir):
        if not scan_dir.is_dir():
            continue
        for f in sorted(scan_dir.rglob("*.jsx")):
            content = _safe_read(f)
            if not content:
                continue
            rel = f.relative_to(repo_root).as_posix()
            imports = []
            for m in import_re.finditer(content):
                imp = m.group(1)
                if imp.startswith(".") or imp.startswith("@/"):
                    imports.append(imp)
            if imports:
                cmap[rel] = imports[:15]  # cap per file
    # Limit total entries to save tokens — pages are most important
    if len(cmap) > 15:
        pages = {k: v for k, v in cmap.items() if "pages/" in k}
        comps = {k: v for k, v in cmap.items() if "pages/" not in k}
        cmap = {**pages, **dict(list(comps.items())[:max(0, 15 - len(pages))])}
    return cmap


def _extract_key_snippets(repo_root: Path, language: str | None) -> dict[str, str]:
    """Read important config/entry files for Claude context."""
    snippets: dict[str, str] = {}
    # Files that give crucial context about the project
    candidates = [
        "package.json",
        "vite.config.js",
        "vite.config.ts",
        "src/App.jsx",
        "src/App.tsx",
        "src/App.js",
        "src/main.jsx",
        "src/main.tsx",
        "src/main.js",
        "src/index.js",
        "src/index.tsx",
        "src/pages.config.js",
        "src/api/db.js",
        "src/lib/utils.js",
        "tailwind.config.js",
        "pyproject.toml",
        "setup.py",
        "src/app.py",
        "src/main.py",
    ]
    count = 0
    for rel in candidates:
        if count >= MAX_SNIPPETS:
            break
        fp = repo_root / rel
        if fp.is_file():
            content = _safe_read(fp, MAX_SNIPPET_BYTES)
            if content:
                snippets[rel] = content
                count += 1
    return snippets


def _detect_entry_points(repo_root: Path, language: str | None) -> list[str]:
    """Identify the main entry points of the application."""
    entry_points: list[str] = []
    for candidate in (
        "src/main.jsx", "src/main.tsx", "src/main.js",
        "src/index.js", "src/index.tsx",
        "src/App.jsx", "src/App.tsx", "src/App.js",
        "index.html",
        "src/app.py", "src/main.py", "app.py", "main.py",
        "src/index.ts", "src/server.ts",
    ):
        if (repo_root / candidate).is_file():
            entry_points.append(candidate)
    return entry_points


def _detect_framework_details(repo_root: Path, language: str | None) -> str | None:
    """Detect the framework stack from manifest files."""
    details: list[str] = []
    pkg_json = repo_root / "package.json"
    if pkg_json.is_file():
        try:
            raw = json.loads(pkg_json.read_text(encoding="utf-8"))
            all_deps = {
                **raw.get("dependencies", {}),
                **raw.get("devDependencies", {}),
            }
            if "react" in all_deps:
                details.append(f"React {all_deps.get('react', '?')}")
            if "next" in all_deps:
                details.append(f"Next.js {all_deps.get('next', '?')}")
            if "vue" in all_deps:
                details.append(f"Vue {all_deps.get('vue', '?')}")
            if "vite" in all_deps:
                details.append(f"Vite {all_deps.get('vite', '?')}")
            if "tailwindcss" in all_deps:
                details.append("Tailwind CSS")
            if "firebase" in all_deps:
                details.append(f"Firebase {all_deps.get('firebase', '?')}")
            if "@tanstack/react-query" in all_deps:
                details.append("React Query")
            if "react-router-dom" in all_deps:
                details.append("React Router")
            if "shadcn" in str(all_deps) or (repo_root / "components.json").is_file():
                details.append("shadcn/ui")
        except (json.JSONDecodeError, OSError):
            pass
    # Check Vercel
    if (repo_root / "vercel.json").is_file():
        details.append("Vercel deployment")
    # Check Firebase config
    for fb_path in ("firebase.js", "src/lib/firebase.js", "lib/firebase.js"):
        if (repo_root / fb_path).is_file():
            details.append(f"Firebase config at {fb_path}")
            break
    return ", ".join(details) if details else None


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
