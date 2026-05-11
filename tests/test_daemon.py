"""Daemon loop tests using fakes (no DB / Claude / GitHub)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

from auto_bug_fixer.daemon import BugFixDaemon
from auto_bug_fixer.health import HealthState


@dataclass
class _FakeSettings:
    poll_interval_seconds: int = 0
    idle_backoff_seconds: int = 0
    error_backoff_seconds: int = 0
    reindex_interval_hours: int = 1
    index_on_startup: bool = False


@dataclass
class _FakePipeline:
    sequence: list[int | Exception]
    calls: list[None] = field(default_factory=list)

    def run_once(self) -> int:
        self.calls.append(None)
        if not self.sequence:
            raise StopIteration
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class _FakeIndexRunner:
    runs: int = 0

    def index_all(self) -> int:
        self.runs += 1
        return 0


def _stop_after(daemon: BugFixDaemon, pipeline: _FakePipeline, n_ticks: int) -> None:
    def waiter() -> None:
        while len(pipeline.calls) < n_ticks:
            pass
        daemon.request_stop()

    threading.Thread(target=waiter, daemon=True).start()


def test_daemon_stops_when_request_stop_is_called() -> None:
    pipeline = _FakePipeline(sequence=[1, 1, 1, 1, 1])
    daemon = BugFixDaemon(settings=_FakeSettings(), pipeline=pipeline)  # type: ignore[arg-type]
    _stop_after(daemon, pipeline, 2)
    assert daemon.run_forever() == 0
    assert len(pipeline.calls) >= 2


def test_daemon_recovers_from_repeated_exceptions() -> None:
    pipeline = _FakePipeline(
        sequence=[RuntimeError("a"), RuntimeError("b"), RuntimeError("c"), 1]
    )
    daemon = BugFixDaemon(settings=_FakeSettings(), pipeline=pipeline)  # type: ignore[arg-type]
    _stop_after(daemon, pipeline, 4)
    assert daemon.run_forever() == 0
    assert len(pipeline.calls) >= 4


def test_idle_backoff_used_when_no_bugs(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _FakePipeline(sequence=[0, 0])
    settings = _FakeSettings(
        poll_interval_seconds=99,
        idle_backoff_seconds=42,
        error_backoff_seconds=77,
    )
    daemon = BugFixDaemon(settings=settings, pipeline=pipeline)  # type: ignore[arg-type]
    sleeps: list[int] = []
    monkeypatch.setattr(daemon, "_sleep_interruptible", lambda s: sleeps.append(s))
    _stop_after(daemon, pipeline, 2)
    daemon.run_forever()
    assert sleeps[0] == 42


def test_error_backoff_used_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _FakePipeline(sequence=[RuntimeError("kaboom"), 1])
    settings = _FakeSettings(
        poll_interval_seconds=10,
        idle_backoff_seconds=20,
        error_backoff_seconds=99,
    )
    daemon = BugFixDaemon(settings=settings, pipeline=pipeline)  # type: ignore[arg-type]
    sleeps: list[int] = []
    monkeypatch.setattr(daemon, "_sleep_interruptible", lambda s: sleeps.append(s))
    _stop_after(daemon, pipeline, 2)
    daemon.run_forever()
    assert sleeps[0] == 99


def test_health_state_records_success_and_error_ticks() -> None:
    """Self-stopping pipeline avoids race conditions on fast runners.

    Tick 1 returns handled=2 (success).
    Tick 2 raises RuntimeError('boom') AND requests daemon shutdown so the
    error tick is the last one and `last_error` is not overwritten by a
    later success/StopIteration.
    """
    health = HealthState()

    @dataclass
    class _StopOnErrorPipeline:
        owner: BugFixDaemon | None = None
        calls: int = 0

        def run_once(self) -> int:
            self.calls += 1
            if self.calls == 1:
                return 2
            if self.owner is not None:
                self.owner.request_stop()
            raise RuntimeError("boom")

    pipeline = _StopOnErrorPipeline()
    daemon = BugFixDaemon(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        pipeline=pipeline,  # type: ignore[arg-type]
        health_state=health,
    )
    pipeline.owner = daemon
    daemon.run_forever()

    snapshot = health.snapshot()
    assert snapshot["total_ticks"] == 2
    assert snapshot["total_handled"] == 2
    assert snapshot["last_error"] == "boom"


def test_index_on_startup_runs_indexer() -> None:
    pipeline = _FakePipeline(sequence=[0])
    runner = _FakeIndexRunner()
    settings = _FakeSettings(index_on_startup=True)
    daemon = BugFixDaemon(
        settings=settings,  # type: ignore[arg-type]
        pipeline=pipeline,
        index_runner=runner,  # type: ignore[arg-type]
    )
    _stop_after(daemon, pipeline, 1)
    daemon.run_forever()
    assert runner.runs >= 1


def test_periodic_reindex_triggers_after_interval() -> None:
    pipeline = _FakePipeline(sequence=[0, 0, 0])
    runner = _FakeIndexRunner()
    settings = _FakeSettings(
        index_on_startup=False,
        reindex_interval_hours=1,
    )
    fake_clock = {"t": 0.0}

    def clock() -> float:
        return fake_clock["t"]

    daemon = BugFixDaemon(
        settings=settings,  # type: ignore[arg-type]
        pipeline=pipeline,
        index_runner=runner,  # type: ignore[arg-type]
        time_source=clock,
    )

    def advance_then_stop() -> None:
        while len(pipeline.calls) < 1:
            pass
        fake_clock["t"] = 3600 * 2
        while runner.runs < 1:
            pass
        daemon.request_stop()

    threading.Thread(target=advance_then_stop, daemon=True).start()
    daemon.run_forever()
    assert runner.runs >= 1


def test_stress_many_iterations_does_not_leak_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_ticks = 100
    health = HealthState()

    @dataclass
    class _StoppingPipeline:
        owner: BugFixDaemon | None = None
        calls: int = 0

        def run_once(self) -> int:
            self.calls += 1
            if self.calls >= target_ticks and self.owner is not None:
                self.owner.request_stop()
            if self.calls % 5 == 0:
                raise RuntimeError("synthetic error")
            return self.calls % 3

    pipeline = _StoppingPipeline()
    daemon = BugFixDaemon(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        pipeline=pipeline,  # type: ignore[arg-type]
        health_state=health,
    )
    pipeline.owner = daemon
    monkeypatch.setattr(daemon, "_sleep_interruptible", lambda _s: None)
    daemon.run_forever()
    snap = health.snapshot()
    assert snap["total_ticks"] == target_ticks
    assert snap["total_handled"] > 0
