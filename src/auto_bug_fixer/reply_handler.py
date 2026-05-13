"""Convert email replies into follow-up Firestore tickets.

When Talya replies to a bot email with feedback like "the blue should
be lighter", this module:
1. Looks up the original bug in Firestore to find the PR branch
2. Creates a new ticket with source_branch so the pipeline fixes
   on the existing PR branch (not main)
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from auto_bug_fixer.config import Settings
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.reply_monitor import ReplyMessage, ReplyMonitor

log = get_logger(__name__)


class ReplyHandler:
    """Check for replies and create follow-up tickets in Firestore."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._monitor = ReplyMonitor(settings)
        self._base_url = (
            f"{settings.firestore_base_url}/projects/{settings.firebase_project_id}"
            f"/databases/(default)/documents/{settings.firestore_collection}"
        )
        self._api_key = settings.firebase_api_key.get_secret_value()

    def process_replies(self) -> int:
        """Check for replies and create follow-up tickets. Returns count."""
        replies = self._monitor.check_replies()
        if not replies:
            return 0

        created = 0
        for reply in replies:
            try:
                # Exact match: "אושר מאשר" → auto-merge the PR
                if reply.feedback.strip() == "אושר מאשר":
                    self._auto_merge(reply)
                    continue
                if self._create_followup(reply):
                    created += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "reply_followup_failed",
                    bug_id=reply.bug_id,
                    error=str(exc),
                )
        log.info("replies_processed", found=len(replies), created=created)
        return created

    def _create_followup(self, reply: ReplyMessage) -> bool:
        """Look up original bug, create follow-up ticket."""
        # Fetch original bug to get PR branch and URL
        original = self._fetch_original(reply.bug_id)
        if original is None:
            log.warning("reply_original_not_found", bug_id=reply.bug_id)
            return False

        pr_url = original.get("pr_url", "")
        project = original.get("project", "")
        email_addr = original.get("email", "")
        original_desc = original.get("description", "")

        if not pr_url:
            log.warning("reply_no_pr_url", bug_id=reply.bug_id)
            return False

        # Extract branch name from PR
        branch = self._get_pr_branch(pr_url)
        if not branch:
            log.warning("reply_no_branch", bug_id=reply.bug_id, pr_url=pr_url)
            return False

        # Build follow-up description
        description = (
            f"המשך לבאג {reply.bug_id}:\n"
            f"{reply.feedback}\n\n"
            f"--- בקשה מקורית ---\n{original_desc[:300]}"
        )

        # Create follow-up ticket in Firestore
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        payload = {
            "fields": {
                "name": {"stringValue": original.get("name", "follow-up")},
                "email": {"stringValue": email_addr},
                "phone": {"stringValue": ""},
                "type": {"stringValue": "bug"},
                "project": {"stringValue": project},
                "description": {"stringValue": description},
                "status": {"stringValue": self._settings.bug_status_new},
                "images": {"arrayValue": {"values": []}},
                "createdAt": {"timestampValue": now},
                "source_branch": {"stringValue": branch},
                "source_pr_url": {"stringValue": pr_url},
                "source_bug_id": {"stringValue": reply.bug_id},
            }
        }

        resp = httpx.post(
            self._base_url,
            params={"key": self._api_key},
            json=payload,
            timeout=15,
        )
        if resp.status_code >= 400:
            log.error(
                "reply_ticket_create_failed",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False

        doc_id = resp.json().get("name", "").rsplit("/", 1)[-1]
        log.info(
            "followup_ticket_created",
            doc_id=doc_id,
            original_bug=reply.bug_id,
            branch=branch,
            feedback_length=len(reply.feedback),
        )
        return True

    def _auto_merge(self, reply: ReplyMessage) -> None:
        """Merge the PR when Talya replies with exactly 'אושר מאשר'."""
        original = self._fetch_original(reply.bug_id)
        if original is None:
            log.warning("merge_original_not_found", bug_id=reply.bug_id)
            return
        pr_url = original.get("pr_url", "")
        if not pr_url:
            log.warning("merge_no_pr_url", bug_id=reply.bug_id)
            return

        parts = pr_url.rstrip("/").split("/")
        if len(parts) < 5 or parts[-2] != "pull":
            log.warning("merge_invalid_pr_url", pr_url=pr_url)
            return
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

        merge_url = (
            f"{self._settings.github_api_url}/repos/{owner}/{repo}"
            f"/pulls/{pr_number}/merge"
        )
        try:
            resp = httpx.put(
                merge_url,
                headers={
                    "Authorization": f"Bearer {self._settings.github_token.get_secret_value()}",
                    "Accept": "application/vnd.github+json",
                },
                json={"merge_method": "squash"},
                timeout=30,
            )
            if resp.status_code < 300:
                log.info(
                    "pr_auto_merged",
                    bug_id=reply.bug_id,
                    pr_url=pr_url,
                    pr_number=pr_number,
                )
                # Update ticket status
                self._update_status(reply.bug_id, "merged")
            else:
                log.warning(
                    "merge_failed",
                    bug_id=reply.bug_id,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
        except (httpx.HTTPError, OSError) as exc:
            log.warning("merge_error", bug_id=reply.bug_id, error=str(exc))

    def _update_status(self, bug_id: str, status: str) -> None:
        """Update ticket status in Firestore."""
        url = f"{self._base_url}/{bug_id}"
        body = {"fields": {"status": {"stringValue": status}}}
        try:
            httpx.patch(
                url,
                params={"key": self._api_key, "updateMask.fieldPaths": ["status"]},
                json=body,
                timeout=15,
            )
        except (httpx.HTTPError, OSError):
            pass

    def _fetch_original(self, bug_id: str) -> dict[str, str] | None:
        """Fetch original bug fields from Firestore."""
        url = f"{self._base_url}/{bug_id}"
        try:
            resp = httpx.get(url, params={"key": self._api_key}, timeout=15)
            if resp.status_code >= 400:
                return None
            fields = resp.json().get("fields", {})
            return {
                k: _string_val(v) for k, v in fields.items()
            }
        except (httpx.HTTPError, OSError):
            return None

    def _get_pr_branch(self, pr_url: str) -> str | None:
        """Extract the head branch from a GitHub PR URL via API."""
        # pr_url like https://github.com/owner/repo/pull/123
        parts = pr_url.rstrip("/").split("/")
        if len(parts) < 5 or parts[-2] != "pull":
            return None
        owner = parts[-4]
        repo = parts[-3]
        pr_number = parts[-1]

        api_url = (
            f"{self._settings.github_api_url}/repos/{owner}/{repo}/pulls/{pr_number}"
        )
        try:
            resp = httpx.get(
                api_url,
                headers={
                    "Authorization": f"Bearer {self._settings.github_token.get_secret_value()}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=15,
            )
            if resp.status_code >= 400:
                return None
            return resp.json().get("head", {}).get("ref")
        except (httpx.HTTPError, OSError):
            return None


def _string_val(field: dict) -> str:
    """Extract string value from a Firestore field."""
    return field.get("stringValue", "")
