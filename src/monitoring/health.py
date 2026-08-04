"""A tiny, dependency-free health & metrics HTTP endpoint.

A long-running bot needs to answer two questions to any monitor:
  * "are you alive and healthy?"  -> ``GET /healthz`` returns 200 + JSON state
  * "how are you doing?"          -> ``GET /metrics`` returns Prometheus text format

Using only the standard library keeps the deploy light: no web framework, no async
runtime. The bot updates a shared :class:`HealthState`; the server just serializes it.
Point Prometheus (see ``deploy/prometheus.yml``) at ``/metrics`` and use ``/healthz`` for
container/orchestrator liveness probes.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import time


@dataclass
class HealthState:
    """Mutable snapshot the bot keeps current; the server reads it on each request."""

    started_at: float = field(default_factory=time)
    last_loop_at: float = 0.0
    loops: int = 0
    equity: float = 0.0
    open_position: float = 0.0
    trades: int = 0
    errors: int = 0
    strategy: str = "unknown"
    symbol: str = "unknown"

    # A component is considered unhealthy if it hasn't ticked within this many seconds.
    max_silence_s: float = 300.0

    def healthy(self) -> bool:
        if self.last_loop_at == 0.0:
            return True  # not yet started its first loop; treat as warming up
        return (time() - self.last_loop_at) < self.max_silence_s

    def to_json(self) -> dict:
        return {
            "healthy": self.healthy(),
            "uptime_s": round(time() - self.started_at, 1),
            "seconds_since_last_loop": round(time() - self.last_loop_at, 1)
            if self.last_loop_at
            else None,
            "loops": self.loops,
            "equity": self.equity,
            "open_position": self.open_position,
            "trades": self.trades,
            "errors": self.errors,
            "strategy": self.strategy,
            "symbol": self.symbol,
        }

    def to_prometheus(self) -> str:
        # Prometheus exposition format: HELP/TYPE comments then metric lines.
        labels = f'strategy="{self.strategy}",symbol="{self.symbol}"'
        lines = [
            "# HELP tradebot_up 1 if the bot is healthy, else 0",
            "# TYPE tradebot_up gauge",
            f"tradebot_up{{{labels}}} {1 if self.healthy() else 0}",
            "# HELP tradebot_equity Current account/paper equity",
            "# TYPE tradebot_equity gauge",
            f"tradebot_equity{{{labels}}} {self.equity}",
            "# HELP tradebot_open_position Current position (units or fraction)",
            "# TYPE tradebot_open_position gauge",
            f"tradebot_open_position{{{labels}}} {self.open_position}",
            "# HELP tradebot_trades_total Trades executed since start",
            "# TYPE tradebot_trades_total counter",
            f"tradebot_trades_total{{{labels}}} {self.trades}",
            "# HELP tradebot_errors_total Errors since start",
            "# TYPE tradebot_errors_total counter",
            f"tradebot_errors_total{{{labels}}} {self.errors}",
            "# HELP tradebot_loops_total Main-loop iterations since start",
            "# TYPE tradebot_loops_total counter",
            f"tradebot_loops_total{{{labels}}} {self.loops}",
        ]
        return "\n".join(lines) + "\n"


class HealthServer:
    """Serve ``/healthz`` and ``/metrics`` for a :class:`HealthState` in a daemon thread."""

    def __init__(self, state: HealthState, host: str = "0.0.0.0", port: int = 8000):
        self.state = state
        self.host = host
        self.port = port
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _make_handler(self):
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: str, content_type: str) -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802 - required signature
                if self.path.startswith("/healthz"):
                    code = 200 if state.healthy() else 503
                    self._send(code, json.dumps(state.to_json()), "application/json")
                elif self.path.startswith("/metrics"):
                    self._send(200, state.to_prometheus(), "text/plain; version=0.0.4")
                else:
                    self._send(404, json.dumps({"error": "not found"}), "application/json")

            def log_message(self, *args):  # silence default stderr access logging
                return

        return Handler

    def start(self) -> None:
        self._httpd = HTTPServer((self.host, self.port), self._make_handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
