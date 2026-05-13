"""Fetch the real Vercel preview URL for a deployment.

Uses the Vercel API to find the preview URL by matching the git commit SHA.
Falls back gracefully if no token or deployment not found.
"""
from __future__ import annotations

import time

import httpx

from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)

VERCEL_API = "https://api.vercel.com"


def get_preview_url(
    vercel_token: str,
    vercel_project_id: str,
    commit_sha: str,
    max_wait: int = 60,
) -> str | None:
    """Wait for and return the Vercel preview URL for a commit.

    Args:
        vercel_token: Vercel API token.
        vercel_project_id: Vercel project ID.
        commit_sha: Git commit SHA to match.
        max_wait: Max seconds to wait for deployment.

    Returns:
        The preview URL or None if not found.
    """
    if not vercel_token or not vercel_project_id:
        return None

    headers = {"Authorization": f"Bearer {vercel_token}"}
    url = f"{VERCEL_API}/v6/deployments?projectId={vercel_project_id}&limit=5"

    # Poll for up to max_wait seconds (Vercel needs time to deploy)
    for attempt in range(max_wait // 10):
        try:
            resp = httpx.get(url, headers=headers, timeout=15)
            if resp.status_code >= 400:
                log.warning("vercel_api_error", status=resp.status_code)
                return None

            for d in resp.json().get("deployments", []):
                sha = d.get("meta", {}).get("githubCommitSha", "")
                if sha == commit_sha and d.get("state") == "READY":
                    preview = f"https://{d['url']}"
                    log.info("vercel_preview_found", url=preview)
                    return preview

            # Not ready yet — wait and retry
            time.sleep(10)
        except (httpx.HTTPError, OSError) as exc:
            log.warning("vercel_api_error", error=str(exc))
            return None

    log.info("vercel_preview_timeout", commit_sha=commit_sha[:10])
    return None


def find_project_id(vercel_token: str, repo_name: str) -> str | None:
    """Find Vercel project ID by GitHub repo name."""
    if not vercel_token:
        return None
    try:
        resp = httpx.get(
            f"{VERCEL_API}/v9/projects",
            headers={"Authorization": f"Bearer {vercel_token}"},
            timeout=15,
        )
        if resp.status_code >= 400:
            return None
        for p in resp.json().get("projects", []):
            if p.get("name") == repo_name:
                return p["id"]
    except (httpx.HTTPError, OSError):
        pass
    return None
