"""Minimal GitHub REST API client (PR creation only)."""
from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from auto_bug_fixer.git_ops.repo import RepoCoordinates
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.models import PullRequest

log = get_logger(__name__)

PR_REQUEST_TIMEOUT_SECONDS = 30


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub REST API returns a non-success response."""


class GitHubClient:
    """Tiny GitHub client that opens pull requests."""

    def __init__(self, token: str, api_url: str) -> None:
        """Bind the client to a token + base API URL."""
        self._token = token
        self._api_url = api_url.rstrip("/")

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(httpx.TransportError),
    )
    def open_pull_request(
        self,
        coords: RepoCoordinates,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequest:
        """Open a pull request and return the parsed result."""
        url = f"{self._api_url}/repos/{coords.owner}/{coords.name}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        with httpx.Client(timeout=PR_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(url, json=payload, headers=headers)

        if response.status_code >= 300:
            raise GitHubAPIError(
                f"PR creation failed: HTTP {response.status_code} {response.text}"
            )
        data = response.json()
        log.info("pr_opened", number=data["number"], url=data["html_url"])
        return PullRequest(
            number=int(data["number"]),
            url=str(data["html_url"]),
            branch=head_branch,
            title=title,
        )
