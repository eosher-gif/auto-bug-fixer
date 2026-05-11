"""Always-on daemon: bug-fix pipeline + periodic re-indexing + health server.

Responsibilities:
- Tick the bug-fix pipeline; back off on idle / error.
- Periodically re-index every repo in the registry so Claude's context stays
  fresh when the codebase evolves.
- Update a thread-safe ``HealthState`` used by the HTTP health endpoint.
- Exit cleanly on SIGINT / SIGTERM between ticks.
"""
from __future__ import annotations

import signal
import threading
import time
from types import FrameType

from auto_bug_fixer.config import Settings
from auto_bug_fixer.health import HealthState
from auto_bug_fixer.indexer.runner import IndexRunner
from auto_bug_fixer.logging_setup import get_logger
from auto_bug_fixer.pipeline import BugFixPipeline

log = get_logger(__name__)


class BugFixDaemon:
    """Long-running loop coordinating the pipeline, indexer, and health state."""

    def __init__(
        self,
        settings: Settings,
        pipeline: BugFixPipeline,
        *,
        index_runner: IndexRunner | None = None,
        health_state: HealthState | None = None,
        time_source=time.monotonic,
    ) -> None:
        """Bind the daemon to its collaborators."""
        self._settings = settings
        self._pipeline = pipeline
        self._index_runner = index_runner
        self._health_state = health_state
        self._stop = threading.Event()
        self._now = time_source
        self._last_reindex: float | None = None

    def request_stop(self, *_: object) -> None:
        """Signal the loop to exit at the next safe boundary."""
        if not self._stop.is_set():
            log.info("shutdown_requested")
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Wire SIGINT and SIGTERM to ``request_stop`` (no-op on unsupported OS)."""
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError) as exc:
                log.warning("signal_install_failed", signal=sig_name, error=str(exc))

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("signal_received", signum=signum)
        self.request_stop()

    def run_forever(self) -> int:
        """Block forever, ticking the pipeline. Returns shell exit code."""
        log.info(
            "daemon_started",
            poll_interval=self._settings.poll_interval_seconds,
            idle_backoff=self._settings.idle_backoff_seconds,
            error_backoff=self._settings.error_backoff_seconds,
            reindex_hours=self._settings.reindex_interval_hours,
        )
        if self._index_runner is not None and self._settings.index_on_startup:
            self._reindex_now()

        while not self._stop.is_set():
            self._maybe_reindex()
            sleep_for = self._tick_once()
            if self._stop.is_set():
                break
            self._sleep_interruptible(sleep_for)
        log.info("daemon_stopped")
        return 0

    def _tick_once(self) -> int:
        """Run one pipeline tick. Returns the number of seconds to sleep next."""
        try:
            handled = self._pipeline.run_once()
        except Exception as exc:
            log.exception("tick_crashed", error=str(exc))
            self._record_health(handled=0, error=str(exc))
            return self._settings.error_backoff_seconds
        self._record_health(handled=handled, error=None)
        if handled == 0:
            return self._settings.idle_backoff_seconds
        return self._settings.poll_interval_seconds

    def _record_health(self, handled: int, error: str | None) -> None:
        if self._health_state is None:
            return
        self._health_state.record_tick(handled=handled, error=error)

    def _maybe_reindex(self) -> None:
        if self._index_runner is None:
            return
        interval = self._settings.reindex_interval_hours * 3600
        now = self._now()
        if self._last_reindex is None or (now - self._last_reindex) >= interval:
            self._reindex_now()

    def _reindex_now(self) -> None:
        if self._index_runner is None:
            return
        try:
            self._index_runner.index_all()
        except Exception as exc:
            log.exception("reindex_failed", error=str(exc))
        self._last_reindex = self._now()

    def _sleep_interruptible(self, seconds: int) -> None:
        """Sleep that returns immediately if shutdown was requested."""
        log.debug("daemon_sleep", seconds=seconds)
        self._stop.wait(timeout=seconds)
