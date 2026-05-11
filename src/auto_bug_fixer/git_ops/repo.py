"""Git repository operations: clone, branch, commit, push.

Uses the ``git`` CLI via subprocess. The GitHub token is injected into the
remote URL only at push time and is never written to disk.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)

GIT_HTTPS_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


class GitOperationError(RuntimeError):
    """Raised when a git command fails."""


@dataclass(frozen=True)
class RepoCoordinates:
    """The owner/name pair extracted from a GitHub HTTPS URL."""

    owner: str
    name: str


def parse_github_url(repo_url: str) -> RepoCoordinates:
    """Extract ``owner`` and ``name`` from an HTTPS GitHub URL."""
    parsed = urlparse(repo_url)
    if parsed.scheme not in {"http", "https"}:
        raise GitOperationError(f"only HTTPS GitHub URLs are supported: {repo_url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise GitOperationError(f"cannot parse owner/repo from URL: {repo_url}")
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    return RepoCoordinates(owner=owner, name=name)


class GitClient:
    """Thin wrapper around the local ``git`` CLI."""

    def __init__(
        self,
        committer_name: str,
        committer_email: str,
        github_token: str,
        timeout_seconds: int,
    ) -> None:
        """Bind a client to a committer identity and auth token."""
        self._committer_name = committer_name
        self._committer_email = committer_email
        self._token = github_token
        self._timeout = timeout_seconds

    def clone(self, repo_url: str, branch: str, dest: Path) -> None:
        """Shallow-clone ``repo_url`` at ``branch`` into ``dest``."""
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                self._with_token(repo_url),
                str(dest),
            ],
            cwd=dest.parent,
        )
        self._run(["git", "config", "user.name", self._committer_name], cwd=dest)
        self._run(["git", "config", "user.email", self._committer_email], cwd=dest)
        log.info("repo_cloned", repo=repo_url, branch=branch, dest=str(dest))

    def create_branch(self, repo_dir: Path, branch_name: str) -> None:
        """Create and switch to a new branch in ``repo_dir``."""
        self._run(["git", "checkout", "-b", branch_name], cwd=repo_dir)

    def commit_all(self, repo_dir: Path, message: str) -> bool:
        """Stage all changes and commit. Returns False if nothing to commit."""
        self._run(["git", "add", "-A"], cwd=repo_dir)
        status = self._run(
            ["git", "status", "--porcelain"], cwd=repo_dir, capture=True
        )
        if not status.strip():
            log.info("nothing_to_commit", repo_dir=str(repo_dir))
            return False
        self._run(["git", "commit", "-m", message], cwd=repo_dir)
        return True

    def push(self, repo_dir: Path, branch_name: str, remote_url: str) -> None:
        """Push ``branch_name`` to ``remote_url`` using the token."""
        self._run(
            [
                "git",
                "push",
                self._with_token(remote_url),
                f"HEAD:{branch_name}",
            ],
            cwd=repo_dir,
        )
        log.info("branch_pushed", branch=branch_name)

    def _with_token(self, url: str) -> str:
        if not GIT_HTTPS_PATTERN.match(url):
            raise GitOperationError(f"only HTTPS remotes are supported: {url}")
        parsed = urlparse(url)
        netloc = f"x-access-token:{self._token}@{parsed.hostname}"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        capture: bool = False,
    ) -> str:
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitOperationError(f"git command timed out: {' '.join(argv)}") from exc

        if completed.returncode != 0:
            raise GitOperationError(
                f"git command failed (exit {completed.returncode}): "
                f"{' '.join(argv)}\nstderr: {completed.stderr.strip()}"
            )
        return completed.stdout if capture else ""
