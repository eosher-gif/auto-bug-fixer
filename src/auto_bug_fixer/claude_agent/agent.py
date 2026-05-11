"""Tool-use loop driving Claude through a bug investigation and fix."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from anthropic import Anthropic, APIError

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
You are an autonomous senior software engineer fixing a customer-reported bug.

Workflow:
1. Use list_dir / read_file / run_cmd to understand the repository.
2. Identify the smallest possible change that fixes the reported bug.
3. Apply changes via write_file (always send the FULL new file content).
4. If a test framework is obvious, run the closest tests with run_cmd.
5. Call the `finish` tool exactly once with a short markdown summary.

Hard rules:
- Make minimal, targeted edits. Do not refactor unrelated code.
- Do not invent files or APIs you have not read.
- Do not modify CI configuration, lockfiles, or dependency versions unless
  the bug is explicitly about them.
- If you cannot reproduce or locate the bug after reasonable investigation,
  call `finish` with a summary explaining what you found and what is missing.
"""


class ClaudeAgentError(RuntimeError):
    """Raised when the agent loop cannot complete."""


class ClaudeBugFixerAgent:
    """Drives Claude with sandboxed tools until it calls ``finish``."""

    def __init__(self, settings: Settings) -> None:
        """Create an agent bound to a configured Anthropic client."""
        self._settings = settings
        self._client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    def fix_bug(
        self,
        bug: Bug,
        repo_root: Path,
        repo_index: RepoIndex | None = None,
    ) -> FixOutcome:
        """Run the agent on ``repo_root`` and return the outcome.

        Args:
            bug: The bug record being processed.
            repo_root: Absolute path to the cloned repo (sandbox root).
            repo_index: Optional pre-built knowledge of the repo, included in
                the initial prompt so Claude starts with the right mental model.
        """
        tools = SandboxedFileTools(repo_root=repo_root)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": _build_initial_user_message(bug, repo_index),
            },
        ]

        for iteration in range(1, self._settings.claude_max_tool_iterations + 1):
            log.info("agent_iteration", bug_id=bug.id, iteration=iteration)
            try:
                response = self._client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=self._settings.claude_max_output_tokens,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )
            except APIError as exc:
                raise ClaudeAgentError(f"Anthropic API error: {exc}") from exc

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


def _build_initial_user_message(bug: Bug, repo_index: RepoIndex | None) -> str:
    context_block = (
        "\n--- Pre-built repository index ---\n"
        f"{repo_index.to_prompt_block()}\n--- end index ---\n"
        if repo_index is not None
        else ""
    )
    return (
        f"Bug ID: {bug.id}\n"
        f"Title: {bug.title}\n"
        f"Repository: {bug.repo_url}\n"
        f"Base branch: {bug.base_branch}\n\n"
        f"--- Customer description ---\n{bug.description}\n--- end ---\n"
        f"{context_block}\n"
        "The repository is already cloned at the sandbox root '.'. "
        "Investigate, fix, and call `finish` when done."
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
