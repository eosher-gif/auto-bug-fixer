"""End-to-end orchestration: DB -> sandbox -> Claude -> PR -> email."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from auto_bug_fixer.claude_agent.agent import ClaudeAgentError, ClaudeBugFixerAgent
from auto_bug_fixer.config import Settings
from auto_bug_fixer.db.firestore_repository import FirestoreBugRepository
from auto_bug_fixer.db.project_resolver import ProjectResolver
from auto_bug_fixer.git_ops.github_api import GitHubAPIError, GitHubClient
from auto_bug_fixer.git_ops.repo import GitClient, GitOperationError, parse_github_url
from auto_bug_fixer.indexer.history_store import HistoryEntry, HistoryStore
from auto_bug_fixer.indexer.index_store import IndexStore
from auto_bug_fixer.indexer.repo_index import RepoIndex
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.models import Bug, FixOutcome, PullRequest
from auto_bug_fixer.notify.email_sender import EmailDeliveryError, EmailNotifier
from auto_bug_fixer.registry import RepoRegistry
from auto_bug_fixer.vercel_preview import find_project_id, get_preview_url

log = get_logger(__name__)

BRANCH_PREFIX = "auto-bug-fixer"


class BugFixPipeline:
    """Coordinates the per-bug processing flow."""

    def __init__(
        self,
        settings: Settings,
        *,
        bug_repository: FirestoreBugRepository | None = None,
        agent: ClaudeBugFixerAgent | None = None,
        git: GitClient | None = None,
        github: GitHubClient | None = None,
        email: EmailNotifier | None = None,
        registry: RepoRegistry | None = None,
        index_store: IndexStore | None = None,
    ) -> None:
        """Construct collaborators from settings, with overrides for tests.

        ``registry`` is required when ``bug_repository`` is not supplied —
        the Firestore repository needs a project resolver, which is built
        from the registry.
        """
        self._settings = settings
        if bug_repository is not None:
            self._repo = bug_repository
        else:
            if registry is None:
                raise ValueError(
                    "registry is required to construct the default "
                    "FirestoreBugRepository"
                )
            self._repo = FirestoreBugRepository(
                settings, ProjectResolver(registry)
            )
        self._agent = agent or ClaudeBugFixerAgent(settings)
        self._git = git or GitClient(
            committer_name=settings.git_committer_name,
            committer_email=settings.git_committer_email,
            github_token=settings.github_token.get_secret_value(),
            timeout_seconds=settings.git_operation_timeout_seconds,
        )
        self._github = github or GitHubClient(
            token=settings.github_token.get_secret_value(),
            api_url=settings.github_api_url,
        )
        self._email = email or EmailNotifier(settings)
        self._registry = registry
        self._index_store = index_store
        self._history = HistoryStore(settings.index_dir) if index_store else None

    def run_once(self) -> int:
        """Process up to ``MAX_BUGS_PER_RUN`` pending bugs. Returns count handled."""
        bugs = self._repo.fetch_pending(self._settings.max_bugs_per_run)
        if not bugs:
            log.info("no_pending_bugs")
            return 0

        self._settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        for bug in bugs:
            self._process_one(bug)
        return len(bugs)

    def _process_one(self, bug: Bug) -> None:
        is_followup = bug.source_branch is not None
        log.info(
            "processing_bug",
            bug_id=bug.id,
            repo=bug.repo_url,
            followup=is_followup,
            source_branch=bug.source_branch,
        )
        self._repo.mark_status(bug.id, self._settings.bug_status_processing)

        sandbox = self._settings.workspace_dir / f"bug-{bug.id}-{int(time.time())}"
        try:
            # Follow-up: clone the PR branch, not main.
            # Fall back to base_branch if the source branch was deleted.
            clone_branch = bug.source_branch or bug.base_branch
            try:
                self._git.clone(bug.repo_url, clone_branch, sandbox)
            except GitOperationError:
                if bug.source_branch:
                    log.warning(
                        "source_branch_gone",
                        branch=bug.source_branch,
                        fallback=bug.base_branch,
                    )
                    self._git.clone(bug.repo_url, bug.base_branch, sandbox)
                else:
                    raise
            repo_index, forbidden_paths, history_block = self._lookup_context(
                bug.repo_url
            )
            outcome = self._agent.fix_bug(
                bug, sandbox,
                repo_index=repo_index,
                forbidden_paths=forbidden_paths,
                history_block=history_block,
            )
            if not outcome.success:
                self._handle_failure(bug, outcome)
                return
            pr = self._publish_fix(bug, sandbox, outcome)
            if pr is None:
                self._handle_failure(
                    bug,
                    FixOutcome(
                        success=False,
                        summary=outcome.summary,
                        changed_files=outcome.changed_files,
                        error="no commit produced after agent finished",
                    ),
                )
                return
            # Fetch Vercel preview URL
            pr.preview_url = self._fetch_vercel_preview(bug, pr)
            self._handle_success(bug, outcome, pr)
        except (GitOperationError, GitHubAPIError, ClaudeAgentError) as exc:
            self._handle_failure(
                bug,
                FixOutcome(success=False, summary="pipeline error", error=str(exc)),
            )
        finally:
            self._cleanup(sandbox)

    def _publish_fix(
        self,
        bug: Bug,
        sandbox: Path,
        outcome: FixOutcome,
    ) -> PullRequest | None:
        if bug.source_branch:
            # Follow-up: push to existing PR branch
            commit_message = (
                f"fix(followup-{bug.id}): {bug.title}\n\n{outcome.summary}"
            )
            if not self._git.commit_all(sandbox, commit_message):
                return None
            sha = _get_head_sha(sandbox)
            self._git.push(sandbox, bug.source_branch, bug.repo_url)
            log.info(
                "followup_pushed",
                bug_id=bug.id,
                branch=bug.source_branch,
                pr_url=bug.source_pr_url,
                sha=sha,
            )
            pr_number = _extract_pr_number(bug.source_pr_url or "")
            return PullRequest(
                number=pr_number,
                url=bug.source_pr_url or "",
                branch=bug.source_branch,
                title=f"[auto] Follow-up fix: {bug.title}",
                commit_sha=sha,
            )

        # New bug: create new branch + PR
        branch = _branch_name(bug.id)
        self._git.create_branch(sandbox, branch)
        commit_message = f"fix(bug-{bug.id}): {bug.title}\n\n{outcome.summary}"
        if not self._git.commit_all(sandbox, commit_message):
            return None
        sha = _get_head_sha(sandbox)
        self._git.push(sandbox, branch, bug.repo_url)

        coords = parse_github_url(bug.repo_url)
        pr_title = f"[auto] Fix bug {bug.id}: {bug.title}"
        pr_body = _render_pr_body(bug, outcome)
        pr = self._github.open_pull_request(
            coords,
            title=pr_title,
            body=pr_body,
            head_branch=branch,
            base_branch=bug.base_branch,
        )
        pr.commit_sha = sha
        return pr

    def _handle_success(
        self,
        bug: Bug,
        outcome: FixOutcome,
        pr: PullRequest,
    ) -> None:
        self._repo.mark_status(bug.id, self._settings.bug_status_mr_opened)
        self._safe_attach(self._repo.attach_pr_url, bug.id, pr.url)
        self._safe_attach(self._repo.attach_ai_notes, bug.id, outcome.summary)
        self._record_history(bug, outcome, pr)
        try:
            self._email.notify_success(bug, outcome, pr)
        except EmailDeliveryError as exc:
            log.error("email_failed", bug_id=bug.id, error=str(exc))

    def _handle_failure(self, bug: Bug, outcome: FixOutcome) -> None:
        log.warning(
            "bug_fix_failed",
            bug_id=bug.id,
            error=outcome.error,
            summary=outcome.summary,
        )
        self._repo.mark_status(bug.id, self._settings.bug_status_failed)
        notes = (outcome.error or outcome.summary or "no notes").strip()
        self._safe_attach(self._repo.attach_ai_notes, bug.id, notes)
        try:
            self._email.notify_failure(bug, outcome)
        except EmailDeliveryError as exc:
            log.error("email_failed", bug_id=bug.id, error=str(exc))

    def _fetch_vercel_preview(self, bug: Bug, pr: PullRequest) -> str | None:
        """Best-effort fetch of Vercel preview URL for the exact commit."""
        token = self._settings.vercel_token.get_secret_value()
        if not token or not pr.commit_sha:
            return None
        try:
            coords = parse_github_url(bug.repo_url)
            project_id = find_project_id(token, coords.name)
            if not project_id:
                return None
            return get_preview_url(token, project_id, pr.commit_sha, max_wait=90)
        except Exception as exc:  # noqa: BLE001
            log.warning("vercel_preview_fetch_failed", error=str(exc))
            return None

    @staticmethod
    def _safe_attach(fn, bug_id: str, value: str) -> None:
        """Best-effort write of an auxiliary field; never blocks the pipeline."""
        try:
            fn(bug_id, value)
        except Exception as exc:  # noqa: BLE001
            log.warning("ticket_attach_failed", bug_id=bug_id, error=str(exc))

    def _record_history(
        self, bug: Bug, outcome: FixOutcome, pr: PullRequest
    ) -> None:
        """Best-effort append of a history entry for this fix."""
        if self._history is None or self._registry is None:
            return
        entry = self._registry.by_url(bug.repo_url)
        if entry is None:
            return
        try:
            from datetime import datetime, timezone

            self._history.append(
                entry,
                HistoryEntry(
                    bug_id=bug.id,
                    ticket_title=bug.title,
                    pr_url=pr.url,
                    pr_number=pr.number,
                    files_touched=outcome.changed_files,
                    ai_summary=outcome.summary,
                    ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("history_append_failed", bug_id=bug.id, error=str(exc))

    def _lookup_context(
        self, repo_url: str
    ) -> tuple[RepoIndex | None, tuple[str, ...], str]:
        """Return the repo index, forbidden paths, and history block."""
        if self._registry is None:
            return None, (), ""
        entry = self._registry.by_url(repo_url)
        if entry is None:
            log.warning("repo_not_in_registry", repo_url=repo_url)
            return None, (), ""
        forbidden = entry.forbidden_paths
        history_block = ""
        if self._history is not None:
            recent = self._history.read_recent(entry, limit=10)
            if recent:
                history_block = self._history.to_prompt_block(recent)
                log.info(
                    "history_loaded",
                    repo_url=repo_url,
                    count=len(recent),
                )
        if self._index_store is None:
            return None, forbidden, history_block
        index = self._index_store.load(entry)
        if index is None:
            log.warning("no_index_available", repo_url=repo_url)
            return None, forbidden, history_block
        log.info(
            "index_loaded",
            repo_url=repo_url,
            indexed_at=index.indexed_at,
            tree_entries=len(index.tree),
            language=index.detected_language,
            forbidden_paths=len(forbidden),
        )
        return index, forbidden, history_block

    @staticmethod
    def _cleanup(sandbox: Path) -> None:
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)


def _get_head_sha(sandbox: Path) -> str:
    """Get the HEAD commit SHA from the local sandbox."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(sandbox), capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.SubprocessError:
        return ""


def _extract_pr_number(pr_url: str) -> int:
    """Extract PR number from a GitHub PR URL."""
    parts = pr_url.rstrip("/").split("/")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0


def _branch_name(bug_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "-" for c in bug_id)
    return f"{BRANCH_PREFIX}/bug-{safe}-{int(time.time())}"


def _render_pr_body(bug: Bug, outcome: FixOutcome) -> str:
    files_block = "\n".join(f"- `{p}`" for p in outcome.changed_files) or "(none)"
    return (
        f"Automated fix for bug **{bug.id}**.\n\n"
        f"### Customer report\n{bug.description}\n\n"
        f"### Summary of change\n{outcome.summary}\n\n"
        f"### Files changed\n{files_block}\n\n"
        f"---\n_Generated by `auto-bug-fixer`. Please review carefully._"
    )
