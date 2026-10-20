import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import apprise
import pytest
from apprise.attachment.memory import AttachMemory

from posteryard import http as posteryard_http
from posteryard.notify import Notifier
from posteryard.server import Item
from posteryard.service import LIVE_TV, Service
from posteryard.webui import api_status
from posteryard.webui.app import WebUi
from posteryard.worker import Restored
from tests.fakes import FakeServer
from tests.test_webui_core import answer, call, make_service


class Titled(FakeServer):
    """Every item exists and is named after its key; key 666 cannot be read."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def item(self, rating_key: str) -> Item | None:
        self.calls.append(rating_key)
        if rating_key == "666":
            raise posteryard_http.RequestError("ConnectionError", "http://plex.example:32400")
        if rating_key == "404":
            return None
        return {"ratingKey": rating_key, "title": f"Title {rating_key}"}


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return make_service(tmp_path, Titled())


@pytest.fixture
def ui(service: Service) -> WebUi:
    assert service.ui is not None
    return service.ui


def get(ui: WebUi, path: str) -> Any:
    response = call(ui, "GET", path)
    assert response.status == 200, response.body
    return answer(response)


# Status


def test_status_has_every_field(ui: WebUi, service: Service) -> None:
    status = get(ui, "/api/status")
    assert set(status) >= {
        "ok", "checks", "version", "dry_run", "paused", "restart_required", "server", "libraries",
        "last_sweep_seconds_ago", "next_sweep_seconds", "sweep_minutes", "daily_at", "last_full_pass",
        "full_pass_running", "queue", "current", "images", "features", "art_sources", "alerts",
    }  # fmt: skip
    assert status["server"] == {"name": "Plex", "url": "http://plex.example:32400"}
    assert status["libraries"] == ["Movies", "TV Shows"]
    assert (status["sweep_minutes"], status["daily_at"]) == (15, "04:15")
    assert status["next_sweep_seconds"] is None and status["current"] is None
    assert status["queue"] == {"total": 0, "by_reason": {}, "held": 0}
    assert status["images"] == {"uploaded": 0, "uploading": 0, "preview": 0, "manual": 0, "failed": 0}
    assert status["restart_required"] is False and status["paused"] is False and status["dry_run"] is True
    assert status["features"] == {
        "fanart": False, "apple_art": False, "text_check": True, "status_labels": True, "maintainerr": False,
        "collection_posters": False, "service_collections": False, "live_tv": False, "notify": False,
        "heartbeat": False, "only_rating_keys": 0,
    }  # fmt: skip
    assert status["art_sources"] == ["TMDB"] and status["alerts"] == []


def test_status_counts_and_timings(ui: WebUi, service: Service) -> None:
    service.store.failed("1", "poster", "One", "boom")
    service.store.uploaded("2", "poster", "Two", "fp", "img")
    service.enqueue(["5", "6"], "daily")
    service.enqueue(["7"], "webhook")
    service.next_sweep_at = time.monotonic() + 100
    ui.restart_required = True
    status = get(ui, "/api/status")
    assert status["images"]["failed"] == 1 and status["images"]["uploaded"] == 1
    assert status["queue"] == {"total": 3, "by_reason": {"webhook": 1, "daily": 2}, "held": 0}
    assert 95 <= status["next_sweep_seconds"] <= 100
    assert status["restart_required"] is True
    service.next_sweep_at = time.monotonic() - 5
    assert get(ui, "/api/status")["next_sweep_seconds"] == 0
    service.pause()
    status = get(ui, "/api/status")
    assert status["paused"] is True and status["next_sweep_seconds"] is None


def test_status_lists_the_features_on(tmp_path: Path) -> None:
    env = {
        "FANART_API_KEY": "example", "APPLE_ART": "true", "STREAMING_REGIONS": "gb,us", "TEXT_CHECK": "false",
        "STATUS_LABELS": "false", "MAINTAINERR_URL": "http://maintainerr.example", "COLLECTION_POSTERS": "true",
        "SERVICE_COLLECTIONS": "true", "NOTIFY_URLS": "json://localhost", "HEARTBEAT_URL": "https://hc.example/1",
        "ONLY_RATING_KEYS": "1,2",
    }  # fmt: skip
    service = make_service(tmp_path, env=env)
    service.live_tv = object()  # type: ignore[assignment]
    assert service.ui is not None
    status = get(service.ui, "/api/status")
    on = {name for name, value in status["features"].items() if value}
    assert on == set(status["features"]) - {"text_check", "status_labels"}
    assert status["features"]["only_rating_keys"] == 2
    assert status["art_sources"] == ["TMDB", "fanart.tv", "Apple TV (GB)"]


def test_the_current_item_is_named(ui: WebUi, service: Service) -> None:
    server = service.server
    assert isinstance(server, Titled)
    service.store.previewed("10", "poster", "Dune", "fp")
    service.current = {"key": "10", "reason": "daily", "started": 5.0}
    assert get(ui, "/api/status")["current"] == {
        "key": "10", "title": "Dune", "reason": "daily", "label": None, "started": 5.0,
    }  # fmt: skip
    assert server.calls == []
    service.current = {"key": "11", "reason": "webhook", "started": 6.0}
    assert get(ui, "/api/status")["current"]["title"] == "Title 11"
    assert get(ui, "/api/status")["current"]["title"] == "Title 11"
    assert server.calls == ["11"]
    for key in ("666", "404"):
        service.current = {"key": key, "reason": "webhook", "started": 6.0}
        assert get(ui, "/api/status")["current"]["title"] is None
    service.current = {"key": "job:1-1", "reason": "ui", "label": "Render Dune", "started": 7.0}
    current = get(ui, "/api/status")["current"]
    assert current["title"] == "Render Dune" and current["label"] == "Render Dune"
    service.current = {"key": LIVE_TV, "reason": "live tv", "started": 8.0}
    assert get(ui, "/api/status")["current"]["title"] == "Live TV"
    assert server.calls == ["11", "666", "404"]


def test_the_title_cache_is_bounded(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_status, "TITLE_CACHE", 2)
    titles = api_status.Titles(service)
    for key in ("1", "2", "3", "1"):
        titles(key)
    server = service.server
    assert isinstance(server, Titled)
    assert server.calls == ["1", "2", "3", "1"]
    assert titles("job:1-1") is None


def test_status_lists_open_alerts(ui: WebUi, service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.worker.notifier, "open_alerts", lambda: [("schedule", 1000.0), ("tmdb", None)])
    expected = [{"cause": "schedule", "since": 1000.0}, {"cause": "tmdb", "since": None}]
    assert get(ui, "/api/status")["alerts"] == expected
    assert get(ui, "/api/problems")["alerts"] == expected


# Actions


def test_sweep_and_full_wake_the_schedule(ui: WebUi, service: Service) -> None:
    assert answer(call(ui, "POST", "/api/actions/sweep")) == {"ok": True}
    assert service._sweep_requested and service._wake.is_set()
    assert answer(call(ui, "POST", "/api/actions/full")) == {"ok": True}
    assert service._full_requested


def test_sweep_and_full_wait_for_a_resume(ui: WebUi, service: Service) -> None:
    service.pause()
    for action in ("sweep", "full"):
        refused = call(ui, "POST", f"/api/actions/{action}")
        assert refused.status == 409 and "paused" in answer(refused)["error"]
    assert not service._sweep_requested and not service._full_requested
    assert call(ui, "GET", "/api/actions/sweep").status == 405


def test_pause_and_resume(ui: WebUi, service: Service) -> None:
    assert answer(call(ui, "POST", "/api/actions/pause")) == {"paused": True}
    assert answer(call(ui, "POST", "/api/actions/pause")) == {"paused": True}
    assert service.paused.is_set() and service.store.meta("paused") == "1"
    service._held.append(("5", "changed"))
    assert get(ui, "/api/status")["queue"]["held"] == 1
    assert answer(call(ui, "POST", "/api/actions/resume")) == {"paused": False}
    assert answer(call(ui, "POST", "/api/actions/resume")) == {"paused": False}
    assert not service.paused.is_set() and service.queue_snapshot() == [(1, "5", "changed")]


class Delivering(Notifier):
    def __init__(self, delivered: bool) -> None:
        super().__init__()
        self.delivered = delivered
        self.sent: list[str] = []

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
        self.sent.append(subject)
        return self.delivered


def job_result(ui: WebUi, path: str, body: object = None) -> Any:
    response = call(ui, "POST", path, body)
    assert response.status == 202, response.body
    job = get(ui, f"/api/jobs/{answer(response)['job']}")
    assert job["state"] == "done", job
    return job["result"]


def test_test_alert(ui: WebUi, service: Service) -> None:
    assert job_result(ui, "/api/actions/test-alert") == {
        "sent": False, "message": "No notification service is set up. Set NOTIFY_URLS.",
    }  # fmt: skip
    service.worker.notifier = Delivering(delivered=True)
    assert job_result(ui, "/api/actions/test-alert") == {"sent": True, "message": "Test alert sent."}
    service.worker.notifier = Delivering(delivered=False)
    result = job_result(ui, "/api/actions/test-alert")
    assert result["sent"] is False and "did not accept" in result["message"]


def test_restore_all_needs_a_pause_and_the_word(ui: WebUi, service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.worker, "restore_all", lambda: Restored(restored=4, kept=1, failed=2))
    assert call(ui, "POST", "/api/actions/restore-all", {"confirm": "RESTORE"}).status == 409
    service.pause()
    for body in (None, {}, {"confirm": "restore"}):
        assert call(ui, "POST", "/api/actions/restore-all", body).status == 400
    result = job_result(ui, "/api/actions/restore-all", {"confirm": "RESTORE"})
    assert result == {"restored": 4, "kept": 1, "failed": 2}


def test_restart_answers_first(ui: WebUi) -> None:
    ui.on_restart = None
    assert call(ui, "POST", "/api/actions/restart").status == 409
    restarted = threading.Event()
    ui.on_restart = restarted.set
    assert answer(call(ui, "POST", "/api/actions/restart")) == {"ok": True}
    assert restarted.wait(5)


def test_restart_reaches_the_service(service: Service) -> None:
    assert service.ui is not None and service.ui.on_restart == service.request_restart


# Queue


def test_the_queue_names_what_waits(ui: WebUi, service: Service) -> None:
    service.store.previewed("7", "poster", "Dune", "fp")
    service.enqueue(["7", "8"], "webhook")
    service.enqueue([LIVE_TV], "live tv")
    job = ui.jobs.dispatch
    ui.jobs.dispatch = service.submit_job
    try:
        ui.jobs.submit("test", "Render Dune", lambda: None)
    finally:
        ui.jobs.dispatch = job
    queue = get(ui, "/api/queue")
    assert queue["total"] == 4 and queue["by_reason"] == {"webhook": 2, "ui": 1, "live tv": 1} and queue["held"] == 0
    items = [(item["reason"], item["priority"], item["title"]) for item in queue["items"]]
    assert items == [("webhook", 0, "Dune"), ("webhook", 0, None), ("ui", 0, "Render Dune"), ("live tv", 1, "Live TV")]
    assert queue["current"] is None
    service._jobs.clear()
    assert get(ui, "/api/queue")["items"][2]["title"] is None


def test_the_queue_lists_the_first_items_only(ui: WebUi, service: Service) -> None:
    service.enqueue([str(n) for n in range(1, 251)], "daily")
    service.current = {"key": "999", "reason": "daily", "started": 1.0}
    queue = get(ui, "/api/queue")
    assert queue["total"] == 250 and len(queue["items"]) == api_status.QUEUE_LIMIT
    assert queue["current"]["title"] == "Title 999"


# Logs


def test_logs_page_by_seq_and_level(ui: WebUi) -> None:
    start = ui.logs.last()
    logger = logging.getLogger("posteryard.tests.webui")
    logger.warning("first", extra={"key": "7"})
    logger.error("second")
    logs = get(ui, f"/api/logs?after={start}&level=warning")
    mine = [r for r in logs["records"] if r["logger"] == "posteryard.tests.webui"]
    assert [r["msg"] for r in mine] == ["first", "second"]
    assert mine[0]["fields"] == {"key": "7"} and mine[0]["level"] == "warning"
    assert logs["last"] >= mine[-1]["seq"]
    errors = get(ui, f"/api/logs?after={start}&level=error")["records"]
    assert "first" not in [r["msg"] for r in errors]
    assert len(get(ui, f"/api/logs?after={start}&level=debug&limit=1")["records"]) == 1
    assert get(ui, f"/api/logs?after={logs['last']}&level=error")["records"] == []
    assert call(ui, "GET", "/api/logs?level=loud").status == 400
    assert call(ui, "GET", "/api/logs?after=x").status == 400


# Activity and problems


def test_activity_pages_newest_first(ui: WebUi, service: Service) -> None:
    store = service.store
    store.uploaded("1", "poster", "One", "fp", "img")
    store.failed("2", "poster", "Two", "TMDB has no poster")
    store.manual("3", "art", "Three")
    for when, key in enumerate(("1", "2", "3")):
        store._db.execute("UPDATE images SET updated_at=? WHERE rating_key=?", (1000 + when, key))
    activity = get(ui, "/api/activity")
    assert activity["total"] == 3 and [r["key"] for r in activity["records"]] == ["3", "2", "1"]
    assert activity["records"][1] == {
        "key": "2", "target": "poster", "title": "Two", "status": "failed", "updated_at": 1001, "failures": 1,
        "last_error": "TMDB has no poster",
    }  # fmt: skip
    assert activity["records"][0]["last_error"] is None
    page = get(ui, "/api/activity?limit=1&offset=1")
    assert page["total"] == 3 and [r["key"] for r in page["records"]] == ["2"]
    failed = get(ui, "/api/activity?status=failed")
    assert failed["total"] == 1 and [r["key"] for r in failed["records"]] == ["2"]
    assert call(ui, "GET", "/api/activity?status=broken").status == 400


def test_problems_list_failed_and_manual(ui: WebUi, service: Service) -> None:
    service.store.failed("2", "poster", "Two", "boom")
    service.store.failed("2", "item", "Two", "gone")
    service.store.manual("3", "poster", "Three")
    service.store.uploaded("4", "poster", "Four", "fp", "img")
    problems = get(ui, "/api/problems")
    assert sorted((r["key"], r["target"], r["last_error"]) for r in problems["failed"]) == [
        ("2", "item", "gone"), ("2", "poster", "boom"),
    ]  # fmt: skip
    assert [r["key"] for r in problems["manual"]] == ["3"] and problems["alerts"] == []


def test_retry_queues_the_keys_given_or_every_failed_one(ui: WebUi, service: Service) -> None:
    assert answer(call(ui, "POST", "/api/problems/retry", {"keys": ["5", "5", "a" * 32]})) == {"queued": 2}
    assert service.queue_snapshot() == [(1, "5", "retry"), (1, "a" * 32, "retry")]
    for bad in ({"keys": "5"}, {"keys": ["../x"]}, {"keys": [5]}):
        assert call(ui, "POST", "/api/problems/retry", bad).status == 400
    service.store.failed("8", "poster", "Eight", "boom")
    service.store.failed("8", "art", "Eight", "boom")
    service.store.failed("9", "item", "Nine", "boom")
    assert answer(call(ui, "POST", "/api/problems/retry", {})) == {"queued": 2}
    assert answer(call(ui, "POST", "/api/problems/retry", {"keys": None})) == {"queued": 2}
    assert {key for _, key, _ in service.queue_snapshot()} == {"5", "a" * 32, "8", "9"}


# Previews


@pytest.fixture
def previews(service: Service) -> Path:
    folder = service.cfg.preview_dir
    folder.mkdir(parents=True)
    names = ["1-poster.jpg", "2-art-blade-runner-2049.jpg", "3-thumb.jpg", "livetv-news-ab12.jpg", "notes.txt",
             "1-cover.jpg", "x-poster.jpg"]  # fmt: skip
    for age, name in enumerate(names):
        path = folder / name
        path.write_bytes(b"\xff\xd8" + name.encode())
        os.utime(path, (1000 - age, 1000 - age))
    (folder / "4-poster.jpg").symlink_to(folder / "missing.jpg")
    service.store.previewed("1", "poster", "One", "fp")
    service.store.uploaded("2", "poster", "Blade Runner 2049", "fp", "img")
    return folder


def test_previews_list_newest_first(ui: WebUi, previews: Path) -> None:
    listed = get(ui, "/api/previews")
    assert listed["total"] == 3
    assert [(i["name"], i["key"], i["target"], i["title"]) for i in listed["items"]] == [
        ("1-poster.jpg", "1", "poster", "One"),
        ("2-art-blade-runner-2049.jpg", "2", "art", "Blade Runner 2049"),
        ("3-thumb.jpg", "3", "thumb", None),
    ]
    first = listed["items"][0]
    assert first["mtime"] == 1000 and first["size"] == len(b"\xff\xd81-poster.jpg")
    assert first["url"] == "/api/previews/1-poster.jpg"
    page = get(ui, "/api/previews?offset=1&limit=1")
    assert page["total"] == 3 and [i["name"] for i in page["items"]] == ["2-art-blade-runner-2049.jpg"]
    art = get(ui, "/api/previews?target=art")
    assert art["total"] == 1 and art["items"][0]["key"] == "2"
    assert call(ui, "GET", "/api/previews?target=cover").status == 400


def test_no_preview_folder_is_no_previews(ui: WebUi) -> None:
    assert get(ui, "/api/previews") == {"total": 0, "items": []}
    assert answer(call(ui, "DELETE", "/api/previews")) == {"deleted": 0}


def test_a_preview_is_served_by_name_only(ui: WebUi, previews: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = call(ui, "GET", "/api/previews/1-poster.jpg")
    assert served.status == 200 and served.content_type == "image/jpeg" and served.body == b"\xff\xd81-poster.jpg"
    assert call(ui, "GET", "/api/previews/livetv-news-ab12.jpg").status == 200
    outside = previews.parent / "secret.jpg"
    outside.write_bytes(b"secret")
    (previews / "link.jpg").symlink_to(outside)
    for bad in ("notes.txt", "..", "..%2Fsecret.jpg", "link.jpg", "4-poster.jpg", "nothing.jpg", "a b.jpg"):
        assert call(ui, "GET", f"/api/previews/{bad}").status == 404, bad
    assert call(ui, "GET", "/api/previews/../secret.jpg").status == 404

    def unreadable(self: Path) -> bytes:
        raise PermissionError(self)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert call(ui, "GET", "/api/previews/1-poster.jpg").status == 404


def test_deleting_previews_keeps_other_files(ui: WebUi, previews: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.unlink

    def stuck(self: Path, missing_ok: bool = False) -> None:
        if self.name == "3-thumb.jpg":
            raise PermissionError(self)
        original(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", stuck)
    assert answer(call(ui, "DELETE", "/api/previews")) == {"deleted": 2}
    assert sorted(p.name for p in previews.iterdir()) == [
        "1-cover.jpg", "3-thumb.jpg", "4-poster.jpg", "livetv-news-ab12.jpg", "notes.txt", "x-poster.jpg",
    ]  # fmt: skip
    assert call(ui, "DELETE", "/api/previews", csrf=False).status == 403


# Live TV guide


def test_the_guide(ui: WebUi, service: Service) -> None:
    assert get(ui, "/api/guide") == {"enabled": False, "items": []}
    service.store.guide_uploaded("ch1", "News", "fp", "img", "https://example.com/news.jpg")
    service.live_tv = object()  # type: ignore[assignment]
    guide = get(ui, "/api/guide")
    assert guide["enabled"] is True
    item = guide["items"][0]
    assert {k: item[k] for k in ("key", "title", "image_key", "picture")} == {
        "key": "ch1", "title": "News", "image_key": "img", "picture": "https://example.com/news.jpg",
    }  # fmt: skip
    assert item["seen_at"] > 0


def test_status_routes_need_a_session(ui: WebUi) -> None:
    for path in ("/api/status", "/api/queue", "/api/logs", "/api/activity", "/api/problems", "/api/previews",
                 "/api/previews/1-poster.jpg", "/api/guide"):  # fmt: skip
        assert call(ui, "GET", path, signed_in=False).status == 401, path
