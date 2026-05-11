"""Tiny stdlib HTTP healthcheck server for liveness / readiness probes.

Endpoints:
- GET ``/health``  -> 200 with JSON snapshot, or 503 if last successful tick
                      is older than ``stale_after_seconds``.
- GET ``/ready``   -> 200 if at least one tick has completed (success or no-op),
                      else 503.

Intentionally synchronous + zero dependencies so it adds no operational risk.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from auto_bug_fixer.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class HealthState:
    """Mutable snapshot updated by the daemon and read by the HTTP server."""

    started_at: float = field(default_factory=time.time)
    last_tick_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    last_handled_count: int = 0
    total_ticks: int = 0
    total_handled: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_tick(self, handled: int, error: str | None) -> None:
        """Record the result of a tick (called by the daemon)."""
        with self._lock:
            now = time.time()
            self.last_tick_at = now
            self.total_ticks += 1
            if error is None:
                self.last_success_at = now
                self.last_handled_count = handled
                self.total_handled += handled
                self.last_error = None
            else:
                self.last_error = error

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of current state."""
        with self._lock:
            return {
                "started_at": self.started_at,
                "uptime_seconds": time.time() - self.started_at,
                "last_tick_at": self.last_tick_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "last_handled_count": self.last_handled_count,
                "total_ticks": self.total_ticks,
                "total_handled": self.total_handled,
            }


def _build_handler(state: HealthState, stale_after_seconds: int) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server convention
            if self.path == "/health":
                self._respond_health()
            elif self.path == "/ready":
                self._respond_ready()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

        def _respond_health(self) -> None:
            snapshot = state.snapshot()
            last_success = snapshot["last_success_at"]
            now = time.time()
            is_healthy = (
                last_success is not None
                and (now - last_success) <= stale_after_seconds
            )
            status = 200 if is_healthy else 503
            payload = {"healthy": is_healthy, **snapshot}
            self._write_json(status, payload)

        def _respond_ready(self) -> None:
            snapshot = state.snapshot()
            ready = snapshot["last_tick_at"] is not None
            self._write_json(200 if ready else 503, {"ready": ready})

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


class HealthServer:
    """Wraps an HTTPServer in a daemon thread; safe to start/stop multiple times."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        state: HealthState,
        stale_after_seconds: int,
    ) -> None:
        """Bind the server to address, state, and freshness threshold."""
        self._host = host
        self._port = port
        self._state = state
        self._stale_after = stale_after_seconds
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        """Return the actual port the server is listening on (after start)."""
        if self._server is None:
            raise RuntimeError("server not started")
        return self._server.server_address[1]

    def start(self) -> None:
        """Bind to (host, port) and serve in a background daemon thread."""
        if self._server is not None:
            return
        handler_cls = _build_handler(self._state, self._stale_after)
        self._server = HTTPServer((self._host, self._port), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="health-server",
            daemon=True,
        )
        self._thread.start()
        log.info("health_server_started", host=self._host, port=self.bound_port)

    def stop(self) -> None:
        """Shut down the server and join its thread."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("health_server_stopped")
        self._server = None
        self._thread = None
