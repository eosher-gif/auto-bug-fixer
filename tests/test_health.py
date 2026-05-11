"""Tests for the HTTP health endpoint."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from urllib.request import Request, urlopen

import pytest

from auto_bug_fixer.health import HealthServer, HealthState


@pytest.fixture
def running_server() -> Iterator[tuple[HealthServer, HealthState]]:
    state = HealthState()
    server = HealthServer(host="127.0.0.1", port=0, state=state, stale_after_seconds=10)
    server.start()
    try:
        yield server, state
    finally:
        server.stop()


def _get(server: HealthServer, path: str) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=5) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "read"):
            body = exc.read().decode("utf-8")
            return int(exc.code), json.loads(body)
        raise


def test_ready_is_503_before_first_tick(running_server) -> None:
    server, _ = running_server
    status, payload = _get(server, "/ready")
    assert status == 503
    assert payload == {"ready": False}


def test_ready_is_200_after_first_tick(running_server) -> None:
    server, state = running_server
    state.record_tick(handled=0, error=None)
    status, payload = _get(server, "/ready")
    assert status == 200
    assert payload == {"ready": True}


def test_health_is_503_before_first_success(running_server) -> None:
    server, _ = running_server
    status, payload = _get(server, "/health")
    assert status == 503
    assert payload["healthy"] is False


def test_health_is_200_after_recent_success(running_server) -> None:
    server, state = running_server
    state.record_tick(handled=2, error=None)
    status, payload = _get(server, "/health")
    assert status == 200
    assert payload["healthy"] is True
    assert payload["last_handled_count"] == 2
    assert payload["total_handled"] == 2


def test_health_goes_stale_when_last_success_too_old() -> None:
    state = HealthState()
    state.record_tick(handled=1, error=None)
    state.last_success_at = time.time() - 9999
    server = HealthServer(host="127.0.0.1", port=0, state=state, stale_after_seconds=10)
    server.start()
    try:
        status, payload = _get(server, "/health")
        assert status == 503
        assert payload["healthy"] is False
    finally:
        server.stop()


def test_unknown_path_returns_404(running_server) -> None:
    server, _ = running_server
    url = f"http://127.0.0.1:{server.bound_port}/nope"
    try:
        with urlopen(Request(url), timeout=5) as response:
            assert response.status == 404
    except Exception as exc:
        assert getattr(exc, "code", None) == 404


def test_record_tick_with_error_updates_last_error() -> None:
    state = HealthState()
    state.record_tick(handled=0, error="boom")
    snapshot = state.snapshot()
    assert snapshot["last_error"] == "boom"
    assert snapshot["last_success_at"] is None
    assert snapshot["total_ticks"] == 1


def test_record_tick_clears_error_on_success() -> None:
    state = HealthState()
    state.record_tick(handled=0, error="boom")
    state.record_tick(handled=1, error=None)
    snapshot = state.snapshot()
    assert snapshot["last_error"] is None
    assert snapshot["total_ticks"] == 2
    assert snapshot["total_handled"] == 1
