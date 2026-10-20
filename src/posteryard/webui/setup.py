"""Setup mode: when the settings do not load, `serve` runs only the web UI so they can be fixed there."""

import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from posteryard.service import RESTART
from posteryard.webui.app import WebUi

log = logging.getLogger(__name__)


class SetupServer:
    def __init__(self, data_dir: Path, password: str, problem: str, port: int, host: str = "0.0.0.0") -> None:
        self.problem = problem
        self.ui = WebUi(data_dir, password, setup_error=problem)
        self.ui.on_restart = self.request_restart
        self.restarting = False
        self._stopping = threading.Event()
        self.httpd = ThreadingHTTPServer((host, port), self.handler())
        self.httpd.daemon_threads = True
        # Do not wait for idle browser connections on the way out: Docker allows 10 seconds.
        self.httpd.block_on_close = False

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def request_restart(self) -> None:
        """Called by POST /api/actions/restart. The handler stops the server once the answer is sent."""
        log.info("restarting to apply new settings")
        self.restarting = True

    def stop(self) -> None:
        if not self._stopping.is_set():
            self._stopping.set()
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def run(self) -> int:
        """Serve until stopped. RESTART when the web UI asked for a restart."""
        self.httpd.serve_forever(poll_interval=0.2)
        self.httpd.server_close()
        return RESTART if self.restarting else 0

    def handler(self) -> type[BaseHTTPRequestHandler]:  # noqa: C901
        setup = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10

            def log_message(self, format: str, *args: Any) -> None:
                """Silent, as in normal mode."""

            def _health(self, method: str) -> None:
                data = json.dumps({"ok": False, "setup": True, "error": setup.problem}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(data)

            def _answer(self, method: str) -> None:
                if self.path.split("?")[0] == "/healthz" and method in ("GET", "HEAD"):
                    self._health(method)
                else:
                    setup.ui.handle(self, method)
                if setup.restarting:
                    setup.stop()

            def do_GET(self) -> None:
                self._answer("GET")

            def do_HEAD(self) -> None:
                self._answer("HEAD")

            def do_POST(self) -> None:
                self._answer("POST")

            def do_PUT(self) -> None:
                self._answer("PUT")

            def do_DELETE(self) -> None:
                self._answer("DELETE")

        return Handler


def serve(data_dir: Path, password: str, problem: str, port: int) -> int:
    """Run setup mode until SIGTERM or SIGINT (0), or until the web UI asks for a restart (RESTART)."""
    server = SetupServer(data_dir, password, problem, port)

    def stop(signum: int, _frame: object) -> None:
        log.info("shutting down", extra={"signal": signal.Signals(signum).name})
        server.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.warning(
        "the settings do not load: open the web UI to fix them",
        extra={"url": f"http://SERVER-IP:{port}", "error": problem},
    )
    return server.run()
