"""Command-line entry point with subcommands.

- ``daemon``     : default; long-running loop (bug-fix + periodic re-index).
- ``index-once`` : one-shot pass over the registry; exits when done.
- ``run-once``   : single pipeline tick; exits when done. Useful for cron / cron-equivalent.
"""
from __future__ import annotations

import argparse
import sys

from auto_bug_fixer.config import Settings, get_settings
from auto_bug_fixer.daemon import BugFixDaemon
from auto_bug_fixer.git_ops.repo import GitClient
from auto_bug_fixer.health import HealthServer, HealthState
from auto_bug_fixer.indexer.index_store import IndexStore
from auto_bug_fixer.indexer.runner import IndexRunner
from auto_bug_fixer.logging_setup import configure_logging, get_logger
from auto_bug_fixer.pipeline import BugFixPipeline
from auto_bug_fixer.registry import RegistryError, RepoRegistry, load_registry


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto_bug_fixer")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("daemon", help="Run the always-on daemon (default).")
    sub.add_parser("index-once", help="Index every repo in the registry and exit.")
    sub.add_parser("run-once", help="Run one pipeline tick and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch to the requested subcommand."""
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger(__name__)

    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    command = args.command or "daemon"
    log.info("auto_bug_fixer_boot", command=command, model=settings.anthropic_model)

    try:
        if command == "daemon":
            return _run_daemon(settings)
        if command == "index-once":
            return _run_index_once(settings)
        if command == "run-once":
            return _run_pipeline_once(settings)
    except Exception as exc:
        log.exception("startup_failed", error=str(exc))
        return 1
    parser.print_help()
    return 2


def _load_registry_or_none(settings: Settings) -> RepoRegistry | None:
    log = get_logger(__name__)
    try:
        return load_registry(settings.repos_file)
    except RegistryError as exc:
        log.warning("registry_unavailable", error=str(exc))
        return None


def _load_registry_or_die(settings: Settings) -> RepoRegistry:
    """Load the registry; exit the process with a clear error if it is missing.

    Pipeline mode (daemon / run-once) cannot operate without a registry —
    every Firestore ticket is mapped to a repo via the project resolver,
    which itself reads from the registry.
    """
    log = get_logger(__name__)
    try:
        return load_registry(settings.repos_file)
    except RegistryError as exc:
        log.error("registry_required", error=str(exc), path=str(settings.repos_file))
        raise SystemExit(1) from exc


def _build_index_runner(
    settings: Settings,
    registry: RepoRegistry | None,
) -> IndexRunner | None:
    if registry is None:
        return None
    git = GitClient(
        committer_name=settings.git_committer_name,
        committer_email=settings.git_committer_email,
        github_token=settings.github_token.get_secret_value(),
        timeout_seconds=settings.git_operation_timeout_seconds,
    )
    return IndexRunner(
        registry=registry,
        store=IndexStore(settings.index_dir),
        git=git,
    )


def _run_daemon(settings: Settings) -> int:
    registry = _load_registry_or_die(settings)
    index_store = IndexStore(settings.index_dir)

    pipeline = BugFixPipeline(
        settings,
        registry=registry,
        index_store=index_store,
    )
    health_state = HealthState() if settings.health_enabled else None
    health_server = (
        HealthServer(
            host=settings.health_host,
            port=settings.health_port,
            state=health_state,
            stale_after_seconds=settings.health_stale_after_seconds,
        )
        if settings.health_enabled and health_state is not None
        else None
    )
    if health_server is not None:
        health_server.start()

    daemon = BugFixDaemon(
        settings=settings,
        pipeline=pipeline,
        index_runner=_build_index_runner(settings, registry),
        health_state=health_state,
    )
    daemon.install_signal_handlers()
    try:
        return daemon.run_forever()
    finally:
        if health_server is not None:
            health_server.stop()


def _run_index_once(settings: Settings) -> int:
    log = get_logger(__name__)
    registry = _load_registry_or_none(settings)
    if registry is None:
        log.error("no_registry_available")
        return 1
    runner = _build_index_runner(settings, registry)
    assert runner is not None
    runner.index_all()
    return 0


def _run_pipeline_once(settings: Settings) -> int:
    log = get_logger(__name__)
    registry = _load_registry_or_die(settings)

    # Check for email replies and create follow-up tickets
    if settings.email_enabled:
        try:
            from auto_bug_fixer.reply_handler import ReplyHandler

            handler = ReplyHandler(settings)
            created = handler.process_replies()
            if created:
                log.info("replies_converted_to_tickets", count=created)
        except Exception as exc:  # noqa: BLE001
            log.warning("reply_check_failed", error=str(exc))

    pipeline = BugFixPipeline(
        settings,
        registry=registry,
        index_store=IndexStore(settings.index_dir),
    )
    pipeline.run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
