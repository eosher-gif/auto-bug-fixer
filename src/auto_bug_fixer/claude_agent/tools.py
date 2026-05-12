"""Sandboxed filesystem + shell tools exposed to the Claude agent.

All paths supplied by the model are resolved against ``repo_root`` and
re-checked to ensure they stay inside the sandbox. Paths that escape
(via ``..`` or absolute paths) are rejected with a clear error.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)

MAX_READ_BYTES = 200_000
MAX_LIST_ENTRIES = 500
DEFAULT_CMD_TIMEOUT_SECONDS = 120
ALLOWED_CMD_PREFIXES = (
    "ls",
    "cat",
    "grep",
    "rg",
    "find",
    "git status",
    "git diff",
    "git log",
    "python",
    "pytest",
    "node",
    "npm test",
    "npm run",
    "yarn",
    "go test",
    "make test",
)


@dataclass
class ToolError(Exception):
    """Raised when a tool call cannot be safely executed."""

    message: str

    def __str__(self) -> str:
        return self.message


class SandboxedFileTools:
    """Filesystem + read-only shell tools confined to a single repo directory."""

    def __init__(
        self,
        repo_root: Path,
        forbidden_paths: tuple[str, ...] = (),
    ) -> None:
        """Bind tools to a specific sandbox root.

        Args:
            repo_root: Absolute path to the cloned repository directory.
            forbidden_paths: Repo-relative paths that must never be written to
                (e.g. ``.env``, ``vercel.json``, ``package-lock.json``).
        """
        if not repo_root.is_absolute():
            raise ToolError(message="repo_root must be absolute")
        self._root = repo_root.resolve()
        self._touched: set[Path] = set()
        self._forbidden = frozenset(forbidden_paths)

    @property
    def changed_files(self) -> list[str]:
        """Return repo-relative POSIX paths of files written by the agent."""
        return sorted(p.relative_to(self._root).as_posix() for p in self._touched)

    def _resolve(self, rel_path: str) -> Path:
        if not rel_path or rel_path.strip() == "":
            raise ToolError(message="path must be non-empty")
        candidate = (self._root / rel_path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ToolError(
                message=f"path '{rel_path}' escapes sandbox root"
            ) from exc
        return candidate

    def list_dir(self, rel_path: str = ".") -> str:
        """List the contents of a directory relative to the sandbox root."""
        target = self._resolve(rel_path)
        if not target.is_dir():
            raise ToolError(message=f"not a directory: {rel_path}")
        entries: list[str] = []
        for idx, child in enumerate(sorted(target.iterdir())):
            if idx >= MAX_LIST_ENTRIES:
                entries.append(f"... (truncated at {MAX_LIST_ENTRIES} entries)")
                break
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.name}{suffix}")
        return "\n".join(entries) if entries else "(empty)"

    def read_file(self, rel_path: str) -> str:
        """Read a UTF-8 text file capped at ``MAX_READ_BYTES``."""
        target = self._resolve(rel_path)
        if not target.is_file():
            raise ToolError(message=f"not a file: {rel_path}")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(
                message=(
                    f"file too large ({size} bytes > {MAX_READ_BYTES}); "
                    "use grep/list_dir to narrow down"
                )
            )
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(message=f"file not UTF-8 decodable: {rel_path}") from exc

    def _check_forbidden(self, rel_path: str) -> None:
        """Raise if ``rel_path`` matches any forbidden path pattern."""
        if not self._forbidden:
            return
        normalized = rel_path.replace("\\", "/").strip("/")
        for forbidden in self._forbidden:
            fb = forbidden.replace("\\", "/").strip("/")
            if normalized == fb or normalized.endswith(f"/{fb}"):
                raise ToolError(
                    message=(
                        f"FORBIDDEN: '{rel_path}' is a protected file and must "
                        f"not be modified. Protected files: {sorted(self._forbidden)}"
                    )
                )

    def write_file(self, rel_path: str, content: str) -> str:
        """Create or overwrite a file with ``content``."""
        target = self._resolve(rel_path)
        self._check_forbidden(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._touched.add(target)
        log.info("agent_wrote_file", path=str(target.relative_to(self._root)))
        return f"wrote {len(content)} chars to {rel_path}"

    def run_cmd(self, command: str, timeout: int = DEFAULT_CMD_TIMEOUT_SECONDS) -> str:
        """Run a read-only/test command from a small allowlist."""
        normalized = command.strip()
        if not any(normalized.startswith(p) for p in ALLOWED_CMD_PREFIXES):
            raise ToolError(
                message=(
                    f"command not allowed: {command!r}. "
                    f"Allowed prefixes: {ALLOWED_CMD_PREFIXES}"
                )
            )
        if not shutil.which(normalized.split()[0]):
            raise ToolError(message=f"executable not found: {normalized.split()[0]}")
        try:
            completed = subprocess.run(  # noqa: S602 - allowlisted prefixes only
                normalized,
                shell=True,
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(message=f"command timed out after {timeout}s") from exc

        stdout_tail = (completed.stdout or "")[-8000:]
        stderr_tail = (completed.stderr or "")[-4000:]
        return (
            f"exit_code={completed.returncode}\n"
            f"--- stdout (tail) ---\n{stdout_tail}\n"
            f"--- stderr (tail) ---\n{stderr_tail}"
        )


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_dir",
        "description": (
            "List the contents of a directory inside the repository. "
            "Use '.' for the repo root."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative directory path.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative file path.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file with the provided content. "
            "Use this to apply your fix."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
                "content": {
                    "type": "string",
                    "description": "Full new file content.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_cmd",
        "description": (
            "Run a read-only or test command inside the repo. "
            "Allowed prefixes: ls, cat, grep, rg, find, git status, git diff, "
            "git log, python, pytest, node, npm test, npm run, yarn, go test, "
            "make test."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Call this exactly once when the fix is complete. Provide a short "
            "summary of the change for the PR description."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Markdown summary of the fix.",
                },
            },
            "required": ["summary"],
        },
    },
]
