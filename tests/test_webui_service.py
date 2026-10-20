import http.client
import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import apprise
import pytest
from apprise.attachment.memory import AttachMemory

from posteryard import http as posteryard_http
from posteryard import notify
from posteryard.notify import Notifier
from posteryard.server import Item
from posteryard.service import RESTART, Service
from posteryard.webui.auth import COOKIE
from posteryard.webui.jobs import Job, JobState
from posteryard.worker import Outcome
from tests.fakes import FakeServer
from tests.test_webui_core import make_service


class Library(FakeServer):
    def sections(self) -> list[Item]:
        return [{"key": "3", "title": "Movies", "type": "movie"}]


def wait_for(check: Callable[[], bool], seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            pytest.fail("timed out")
        time.sleep(0.01)


@pytest.fixture
def working(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Service, list[str]]]:
    """A service whose worker thread runs, recording the library items it processes."""
    monkeypatch.setattr("posteryard.service.TICK", 0.01)
    service = make_service(tmp_path, sync=False)
    processed: list[str] = []

    def process(key: str, **_kwargs: Any) -> Outcome:
        processed.append(key)
        return Outcome.UNCHANGED

    monkeypatch.setattr(service.worker, "process", process)
    monkeypatch.setattr(service.worker, "rest", lambda: None)
    service.server_checked.set()
    thread = threading.Thread(target=service._work, daemon=True)
    thread.start()
    yield service, processed
    service.stop()
    thread.join(timeout=5)


def test_a_job_runs_on_the_worker_thread(working: tuple[Service, list[str]]) -> None:
    service, _ = working
    assert service.ui is not None
    seen: list[tuple[str, dict[str, Any] | None]] = []

    def run() -> str:
        seen.append((threading.current_thread().name, service.current))
        return "done"

    job = service.ui.jobs.submit("test", "Render Dune", run)
    wait_for(lambda: job.state == JobState.DONE)
    assert job.result == "done"
    name, current = seen[0]
    assert name != threading.main_thread().name
    assert current is not None and current["reason"] == "ui" and current["label"] == "Render Dune"
    assert current["key"] == f"job:{job.id}"
    wait_for(lambda: service.current is None)


def test_a_job_waits_in_the_queue_and_is_named_there(tmp_path: Path) -> None:
    service = make_service(tmp_path, sync=False)
    assert service.ui is not None
    job = service.ui.jobs.submit("test", "Render", lambda: None)
    key = f"job:{job.id}"
    assert service.queue_snapshot() == [(0, key, "ui")]
    assert service.queued_job(key) is job and service.queued_job("job:none") is None
    assert job.state == JobState.QUEUED


def test_a_job_without_the_web_ui_is_dropped(tmp_path: Path) -> None:
    service = make_service(tmp_path, sync=False)
    job = Job("1-1", "test", "Test", lambda: pytest.fail("must not run"))
    service.submit_job(job)
    service.ui = None
    service._run_job(f"job:{job.id}")
    service._run_job("job:unknown")
    assert job.state == JobState.QUEUED and service.current is None


def test_a_pause_holds_library_items_but_not_jobs(working: tuple[Service, list[str]]) -> None:
    service, processed = working
    assert service.ui is not None
    service.pause()
    assert service.store.meta("paused") == "1" and service.status()["paused"] is True
    service.enqueue(["1", "2"], "changed")
    job = service.ui.jobs.submit("test", "Test", lambda: "ran")
    wait_for(lambda: job.state == JobState.DONE and service.held() == 2)
    assert processed == [] and service.queue_snapshot() == []
    service.resume_work()
    assert service.store.meta("paused") == "0" and service.status()["paused"] is False
    wait_for(lambda: processed == ["1", "2"])
    assert service.held() == 0


def test_a_pause_survives_a_restart(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.pause()
    service.store.close()
    assert make_service(tmp_path).paused.is_set()


def test_a_restart_request_stops_with_the_restart_code(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.request_restart()
    assert service.exit_code == RESTART == 3
    assert service._stop.is_set()


@pytest.fixture
def scheduled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Service, list[str]]]:
    """A schedule thread with a long tick: only a wake-up makes it run again soon."""
    monkeypatch.setattr("posteryard.service.TICK", 60)
    service = make_service(tmp_path, Library())
    runs: list[str] = []
    monkeypatch.setattr(service, "sweep", lambda lookback=0: runs.append("sweep"))
    monkeypatch.setattr(service, "full", lambda reason: runs.append(f"full {reason}"))
    service.store.set_meta("settings_signature", service.settings_signature())
    service.store.set_meta("last_full", date.today().isoformat())
    thread = threading.Thread(target=service._schedule, daemon=True)
    thread.start()
    wait_for(lambda: runs == ["sweep"])
    yield service, runs
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_requested_sweep_runs_on_the_schedule_thread(scheduled: tuple[Service, list[str]]) -> None:
    service, runs = scheduled
    first = service.next_sweep_at
    assert first is not None
    service.request_sweep()
    wait_for(lambda: runs == ["sweep", "sweep"])
    assert service.next_sweep_at is not None and service.next_sweep_at > first
    service.request_full()
    wait_for(lambda: runs == ["sweep", "sweep", "full web UI"])


def test_a_stop_wakes_the_schedule_at_once(scheduled: tuple[Service, list[str]]) -> None:
    service, _ = scheduled
    started = time.monotonic()
    service.stop()
    assert time.monotonic() - started < 5


def test_a_paused_schedule_still_stops_at_once(scheduled: tuple[Service, list[str]]) -> None:
    service, runs = scheduled
    service.pause()
    service._wake.set()
    time.sleep(0.05)
    service.request_sweep()
    time.sleep(0.05)
    assert runs == ["sweep"]


def test_a_request_skips_the_wait_after_a_failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("posteryard.service.TICK", 60)
    service = make_service(tmp_path, Library())
    calls: list[int] = []

    def sweep(lookback: int = 0) -> None:
        calls.append(lookback)
        if len(calls) == 1:
            raise posteryard_http.RequestError("ConnectionError", "http://plex.example:32400")

    monkeypatch.setattr(service, "sweep", sweep)
    service.store.set_meta("settings_signature", service.settings_signature())
    service.store.set_meta("last_full", date.today().isoformat())
    thread = threading.Thread(target=service._schedule, daemon=True)
    thread.start()
    try:
        wait_for(lambda: len(calls) == 1)
        service._wake.set()
        time.sleep(0.05)
        assert len(calls) == 1
        service.request_sweep()
        wait_for(lambda: len(calls) == 2)
    finally:
        service.stop()
        thread.join(timeout=5)


class Delivered(Notifier):
    def __init__(self, state: notify.State) -> None:
        super().__init__(state=state)

    @property
    def configured(self) -> bool:
        return True

    def send(
        self,
        subject: str,
        message: str,
        kind: apprise.NotifyType = apprise.NotifyType.WARNING,
        attach: AttachMemory | None = None,
    ) -> bool:
        return True


class Meta:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def meta(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.values[key] = value


def test_open_alerts_and_when_they_went_out() -> None:
    state = Meta()
    notifier = Delivered(state)
    assert notifier.open_alerts() == []
    before = time.time()
    notifier.alert("tmdb", "TMDB does not answer")
    notifier.alert("server", "Plex does not answer")
    opened = notifier.open_alerts()
    assert [cause for cause, _ in opened] == ["server", "tmdb"]
    assert all(since is not None and before <= since <= time.time() for _, since in opened)
    reloaded = Delivered(state).open_alerts()
    assert [cause for cause, _ in reloaded] == ["server", "tmdb"]
    assert all(since == pytest.approx(dict(opened)[cause]) for cause, since in reloaded)
    notifier.alert("tmdb", "TMDB does not answer")
    assert notifier.open_alerts() == opened
    notifier.resolve("tmdb", "TMDB answers again")
    assert notifier.open_alerts() == opened[:1]
    saved = json.loads(state.values[notify.ALERT_STATE])
    assert set(saved) == {"muted", "open"}


def test_an_unknown_time_stays_unknown() -> None:
    state = Meta()
    state.set_meta(notify.ALERT_STATE, json.dumps({"muted": {}, "open": ["tmdb"]}))
    assert Delivered(state).open_alerts() == [("tmdb", None)]


def test_the_http_server_routes_the_web_ui(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assert service.ui is not None
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def send(method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        data = response.read()
        frame = response.getheader("X-Frame-Options")
        connection.close()
        return response.status, (json.loads(data) if data.startswith(b"{") else data, frame)

    cookie = {"Cookie": f"{COOKIE}={service.ui.auth.issue()}", "X-Requested-With": "posteryard"}
    try:
        status, (health, frame) = send("GET", "/healthz?x=1")
        assert status == 200 and health["paused"] is False and frame is None
        assert send("POST", "/webhook/example-secret", b"{}", {"Content-Type": "application/json"})[0] == 200
        status, (session, frame) = send("GET", "/api/session")
        assert status == 200 and "authenticated" in session and frame == "DENY"
        assert send("GET", "/library")[0] == 200
        assert send("HEAD", "/library")[0] == 200
        assert send("POST", "/api/logout", headers=cookie)[0] == 200
        assert send("PUT", "/api/nothing", headers=cookie)[0] == 404
        assert send("DELETE", "/api/nothing", headers=cookie)[0] == 404
        service.ui = None
        for method in ("GET", "POST", "PUT", "DELETE"):
            status, (body, _) = send(method, "/api/session", headers=cookie)
            assert status == 404 and body == {"error": "not found"}, method
        assert send("HEAD", "/library")[0] == 404
        assert send("HEAD", "/healthz")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_without_web_ui_no_ui_is_attached(tmp_path: Path) -> None:
    service = make_service(tmp_path / "off", env={"WEB_UI": "false"})
    assert service.ui is None
    assert "off, WEB_UI=false" in service.banner()
