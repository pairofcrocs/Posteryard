import http.client
import json
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from posteryard import cli, config
from posteryard.service import RESTART
from posteryard.webui import logbuffer, setup
from posteryard.webui.routing import Request
from posteryard.webui.setup import SetupServer
from tests.test_lookup import FakePlex

PROBLEM = "TMDB_API_KEY is required"
WORKING = {
    "TMDB_API_KEY": "tmdb", "PLEX_URL": "http://plex.example:32400", "PLEX_TOKEN": "token", "WEBHOOK_SECRET": "hook",
}  # fmt: skip
Reply = tuple[int, dict[str, str], Any]


@pytest.fixture(autouse=True)
def keep_log_handlers() -> Iterator[None]:
    """cli.main replaces the root handlers, which would drop the web UI's log buffer for later tests."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    buffer = logbuffer.install()
    root.handlers[:] = [*handlers, *([buffer] if buffer not in handlers else [])]
    root.setLevel(level)


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [*config.SECRETS, *(f"{n}_FILE" for n in config.SECRETS), "PLEX_URL", "WEB_UI", "LISTEN_PORT"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def running(clean_env: Path) -> Iterator[tuple[SetupServer, list[int]]]:
    server = SetupServer(clean_env, "", PROBLEM, 0, host="127.0.0.1")
    codes: list[int] = []
    thread = threading.Thread(target=lambda: codes.append(server.run()), daemon=True)
    thread.start()
    yield server, codes
    server.stop()
    thread.join(5)
    assert not thread.is_alive()


def request(server: SetupServer, method: str, path: str, body: Any = None, cookie: str = "") -> Reply:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"X-Requested-With": "posteryard", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
    finally:
        connection.close()


def test_setup_mode_answers_health_checks_and_the_session(running: tuple[SetupServer, list[int]]) -> None:
    server, _ = running
    assert request(server, "GET", "/healthz")[::2] == (503, {"ok": False, "setup": True, "error": PROBLEM})
    assert request(server, "HEAD", "/healthz")[0] == 503
    status, _, session = request(server, "GET", "/api/session")
    assert status == 200
    assert (session["setup"], session["setup_error"], session["password_set"]) == (True, PROBLEM, False)
    assert request(server, "GET", "/api/status")[0] == 404
    assert request(server, "POST", "/webhook/anything")[0] == 404


def test_the_first_visitor_chooses_the_password(running: tuple[SetupServer, list[int]], clean_env: Path) -> None:
    server, codes = running
    status, _, data = request(server, "GET", "/api/settings")
    assert (status, data["error"]) == (200, PROBLEM)
    status, _, data = request(server, "PUT", "/api/settings", {"values": WORKING})
    assert (status, data) == (400, {"error": "Choose a password for the web UI first"})
    assert not (clean_env / "settings.json").exists()

    status, headers, data = request(server, "PUT", "/api/settings", {"values": {"UI_PASSWORD": "chosen"}})
    assert (status, data["error"], data["restart_required"]) == (200, PROBLEM, True)
    assert server.ui.auth.check_password("chosen")
    cookie = headers["Set-Cookie"].split(";")[0]

    # From now on, the settings need the session.
    assert request(server, "GET", "/api/settings")[0] == 401
    assert request(server, "PUT", "/api/settings", {"values": {"UI_PASSWORD": "mine"}})[0] == 401
    assert request(server, "POST", "/api/settings/test", {"what": "tmdb"})[0] == 401
    assert request(server, "POST", "/api/actions/restart")[0] == 401
    status, headers, data = request(server, "PUT", "/api/settings", {"values": WORKING}, cookie=cookie)
    assert (status, data["error"]) == (200, None)
    assert "Set-Cookie" not in headers
    assert config.saved_settings(clean_env) == {**WORKING, "UI_PASSWORD": "chosen"}
    assert request(server, "POST", "/api/actions/restart", cookie=cookie)[::2] == (200, {"ok": True})
    for _ in range(50):
        if codes:
            break
        threading.Event().wait(0.1)
    assert codes == [RESTART]


def test_setup_mode_saves_settings_that_do_not_load_yet(clean_env: Path) -> None:
    server = SetupServer(clean_env, "", PROBLEM, 0, host="127.0.0.1")
    try:
        body = {"values": {"UI_PASSWORD": "chosen", "DAILY_AT": "25:00"}}
        response = server.ui.dispatch(setup_request("PUT", "/api/settings", body))
        assert response.status == 200
        assert json.loads(response.body)["error"] == PROBLEM
        assert config.saved_settings(clean_env)["DAILY_AT"] == "25:00"
    finally:
        server.httpd.server_close()


def test_a_password_from_the_environment_is_needed_in_setup_mode(clean_env: Path) -> None:
    server = SetupServer(clean_env, "hook", PROBLEM, 0, host="127.0.0.1")
    try:
        response = server.ui.dispatch(
            setup_request("PUT", "/api/settings", {"values": {**WORKING, "UI_PASSWORD": "mine"}})
        )
        assert response.status == 401
        assert not (clean_env / "settings.json").exists()
    finally:
        server.httpd.server_close()


def setup_request(method: str, path: str, body: Any) -> Request:
    return Request(method, path, {}, {"X-Requested-With": "posteryard"}, json.dumps(body).encode())


def test_a_signal_stops_setup_mode(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[int, Callable[[int, object], None]] = {}
    monkeypatch.setattr(signal, "signal", handlers.setdefault)
    started = threading.Event()
    original = SetupServer.run

    def run(self: SetupServer) -> int:
        started.set()
        return original(self)

    monkeypatch.setattr(SetupServer, "run", run)
    timer = threading.Thread(target=lambda: started.wait(5) and handlers[signal.SIGTERM](signal.SIGTERM, None))
    timer.start()
    assert setup.serve(clean_env, "", PROBLEM, 0) == 0
    timer.join(5)
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}


# The serve command


@pytest.fixture
def restarts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def restart() -> int:
        calls.append("restart")
        return 99

    monkeypatch.setattr(cli, "restart", restart)
    return calls


def test_serve_without_settings_runs_setup_mode(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, restarts: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[Any, ...]] = []

    def serve(data_dir: Path, password: str, problem: str, port: int) -> int:
        seen.append((data_dir, password, problem, port))
        return RESTART

    monkeypatch.setattr(setup, "serve", serve)
    monkeypatch.setenv("LISTEN_PORT", "nope")
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    assert cli.main(["serve"]) == 99
    assert seen == [(clean_env, "hook", PROBLEM, 8000)]
    assert restarts == ["restart"]
    assert PROBLEM in capsys.readouterr().out

    monkeypatch.setattr(setup, "serve", lambda *args: 0)
    assert cli.main(["serve"]) == 0
    assert restarts == ["restart"]


def test_serve_setup_mode_reports_a_busy_port(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def busy(*args: Any) -> int:
        raise OSError(48, "Address already in use")

    monkeypatch.setattr(setup, "serve", busy)
    assert cli.main(["serve"]) == 2
    assert "The web UI could not start on port 8000" in capsys.readouterr().out


def test_serve_with_the_web_ui_off_exits_as_before(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WEB_UI", "false")
    monkeypatch.setattr(setup, "serve", lambda *args: pytest.fail("setup mode ran"))
    assert cli.main(["serve"]) == 2
    assert capsys.readouterr().out.strip() == PROBLEM


def test_serve_starts_again_when_the_service_asks(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, restarts: list[str]
) -> None:
    for name, value in WORKING.items():
        monkeypatch.setenv(name, value)
    config.save_settings(clean_env, {"LOG_LEVEL": "warning"})
    used: list[config.Config] = []

    class Service:
        def __init__(self, cfg: config.Config, *args: Any) -> None:
            used.append(cfg)

        def run(self) -> int:
            return RESTART

    monkeypatch.setattr(cli, "Plex", lambda url, token: FakePlex())
    monkeypatch.setattr(cli, "Service", Service)
    assert cli.main(["serve"]) == 99
    assert restarts == ["restart"]
    assert used[0].log_level == config.LogLevel.WARNING


def test_restart_runs_serve_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    cli.restart()
    assert calls == [(sys.executable, [sys.executable, "-m", "posteryard", "serve"])]


def test_health_works_without_settings(clean_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LISTEN_PORT", str(port))
        assert cli.main(["health"]) == 1
    assert f"not reachable on port {port}" in capsys.readouterr().out
