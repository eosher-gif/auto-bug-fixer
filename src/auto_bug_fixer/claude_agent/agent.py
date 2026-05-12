"""Tool-use loop driving Claude through a bug investigation and fix."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import time

from anthropic import Anthropic, APIError, RateLimitError

from auto_bug_fixer.claude_agent.tools import (
    TOOL_SCHEMAS,
    SandboxedFileTools,
    ToolError,
)
from auto_bug_fixer.config import Settings
from auto_bug_fixer.indexer.repo_index import RepoIndex
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.models import Bug, FixOutcome

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You fix bugs in a small React+Firebase+Vite Hebrew storefront.

Strategy (follow exactly):
1. The page list in the context tells you where to look. If the bug says
   "Login page" → read src/pages/Login.jsx. "Dashboard" → src/pages/Dashboard.jsx.
2. list_dir src/pages and src/components ONLY if you can't guess the file.
3. read_file the target file. Find the bug. Fix it.
4. write_file with the FULL fixed file content.
5. Call `finish` immediately with a one-line Hebrew-friendly summary.

Do NOT: explore broadly, read more than 3 files, refactor, or touch
.env / firebase.js / vercel.json / package-lock.json / yarn.lock.
"""


class ClaudeAgentError(RuntimeError):
    """Raised when the agent loop cannot complete."""


class ClaudeBugFixerAgent:
    """Drives Claude with sandboxed tools until it calls ``finish``."""

    MAX_RATE_LIMIT_RETRIES = 5
    RATE_LIMIT_BASE_WAIT = 30  # seconds

    def __init__(self, settings: Settings) -> None:
        """Create an agent bound to a configured Anthropic client."""
        self._settings = settings
        self._client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    def _call_with_retry(self, messages: list[dict[str, Any]]):
        """Call the API with automatic retry on rate limits."""
        for attempt in range(self.MAX_RATE_LIMIT_RETRIES):
            try:
                return self._client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=self._settings.claude_max_output_tokens,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )
            except RateLimitError as exc:
                wait = self.RATE_LIMIT_BASE_WAIT * (attempt + 1)
                log.warning(
                    "rate_limited",
                    attempt=attempt + 1,
                    wait_seconds=wait,
                    error=str(exc),
                )
                time.sleep(wait)
            except APIError as exc:
                raise ClaudeAgentError(f"Anthropic API error: {exc}") from exc
        return None

    def fix_bug(
        self,
        bug: Bug,
        repo_root: Path,
        repo_index: RepoIndex | None = None,
        forbidden_paths: tuple[str, ...] = (),
        history_block: str = "",
    ) -> FixOutcome:
        """Run the agent on ``repo_root`` and return the outcome.

        Args:
            bug: The bug record being processed.
            repo_root: Absolute path to the cloned repo (sandbox root).
            repo_index: Optional pre-built knowledge of the repo, included in
                the initial prompt so Claude starts with the right mental model.
            forbidden_paths: Repo-relative paths that must never be written to.
            history_block: Pre-rendered prompt section with past fixes.
        """
        tools = SandboxedFileTools(
            repo_root=repo_root,
            forbidden_paths=forbidden_paths,
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": _build_initial_user_message(
                    bug, repo_index, forbidden_paths, history_block
                ),
            },
        ]

        for iteration in range(1, self._settings.claude_max_tool_iterations + 1):
            log.info("agent_iteration", bug_id=bug.id, iteration=iteration)
            response = self._call_with_retry(messages)

            if response is None:
                raise ClaudeAgentError("Anthropic API: exhausted retries on rate limit")

            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                log.warning("agent_no_tool_use", bug_id=bug.id)
                return FixOutcome(
                    success=False,
                    summary="agent stopped without calling finish",
                    changed_files=tools.changed_files,
                    error="no tool use in response",
                )

            tool_results: list[dict[str, Any]] = []
            finish_summary: str | None = None
            for block in tool_uses:
                if block.name == "finish":
                    finish_summary = str(block.input.get("summary", "")).strip()
                    tool_results.append(
                        _tool_result(block.id, "fix recorded", is_error=False)
                    )
                    continue
                output, is_error = _dispatch_tool(tools, block.name, block.input)
                tool_results.append(_tool_result(block.id, output, is_error))

            messages.append({"role": "user", "content": tool_results})

            if finish_summary is not None:
                changed = tools.changed_files
                if not changed:
                    return FixOutcome(
                        success=False,
                        summary=finish_summary,
                        changed_files=[],
                        error="agent finished without changing any file",
                    )
                return FixOutcome(
                    success=True,
                    summary=finish_summary,
                    changed_files=changed,
                )

        return FixOutcome(
            success=False,
            summary="iteration limit reached before finish",
            changed_files=tools.changed_files,
            error=f"hit max_iterations={self._settings.claude_max_tool_iterations}",
        )


def _build_initial_user_message(
    bug: Bug,
    repo_index: RepoIndex | None,
    forbidden_paths: tuple[str, ...] = (),
    history_block: str = "",
) -> str:
    context = repo_index.to_prompt_block() if repo_index else ""
    history = f"\nPast fixes:\n{history_block}" if history_block else ""
    return (
        f"Fix this bug (ID: {bug.id}):\n"
        f"{bug.description}\n\n"
        f"{context}{history}\n"
        "Repo is cloned at '.'. Read the relevant file, fix it, call finish."
    )


def _dispatch_tool(
    tools: SandboxedFileTools,
    name: str,
    raw_input: dict[str, Any],
) -> tuple[str, bool]:
    try:
        if name == "list_dir":
            return tools.list_dir(str(raw_input.get("path", "."))), False
        if name == "read_file":
            return tools.read_file(str(raw_input["path"])), False
        if name == "write_file":
            return (
                tools.write_file(str(raw_input["path"]), str(raw_input["content"])),
                False,
            )
        if name == "run_cmd":
            return tools.run_cmd(str(raw_input["command"])), False
        return f"unknown tool: {name}", True
    except ToolError as exc:
        return f"tool_error: {exc}", True
    except KeyError as exc:
        return f"missing required argument: {exc}", True


def _tool_result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }
