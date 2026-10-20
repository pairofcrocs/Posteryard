"""Web UI API: status, actions, queue, logs, activity, problems, previews and the guide. See docs/webui-api.md."""

import dataclasses
import logging
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from posteryard import http
from posteryard.server import is_item_key
from posteryard.service import JOB_PREFIX, LIVE_TV, Service
from posteryard.store import Record, Status, Store
from posteryard.webui.logbuffer import ORDER
from posteryard.webui.routing import ApiError, Request, Response, json_response

if TYPE_CHECKING:
    from posteryard.webui.app import WebUi

log = logging.getLogger(__name__)
QUEUE_LIMIT = 200
PROBLEM_LIMIT = 500
TITLE_CACHE = 500
RESTART_DELAY = 0.3
TARGETS = ("poster", "art", "thumb")
# Previews the service saves (`<key>-<target>.jpg`) and the ones `posteryard preview` saves (`...-<slug>.jpg`).
PREVIEW_NAME = re.compile(r"^(?P<key>\d{1,12}|[0-9a-f]{32})-(?P<target>poster|art|thumb)(?:-[a-z0-9-]+)?\.jpg$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.jpg$")
LIVE_TV_TITLE = "Live TV"
PAUSED = "Posteryard is paused. Resume it first."


def record_json(store: Store, record: Record) -> dict[str, Any]:
    return {
        "key": record.rating_key, "target": record.target, "title": record.title, "status": record.status,
        "updated_at": record.updated_at, "failures": record.failures,
        "last_error": store.last_error(record.rating_key, record.target) or None,
    }  # fmt: skip


def stored_title(store: Store, key: str) -> str | None:
    found = store.records(rating_key=key, limit=1)
    return (found[0].title or None) if found else None


def features(service: Service) -> dict[str, Any]:
    cfg = service.cfg
    return {
        "fanart": bool(cfg.fanart_api_key), "apple_art": cfg.apple_art, "text_check": cfg.text_check,
        "status_labels": cfg.status_labels, "maintainerr": bool(cfg.maintainerr_url),
        "collection_posters": cfg.collection_posters, "service_collections": cfg.service_collections,
        "live_tv": service.live_tv is not None, "notify": bool(cfg.notify_urls), "heartbeat": bool(cfg.heartbeat_url),
        "only_rating_keys": len(cfg.only_rating_keys),
    }  # fmt: skip


def art_sources(service: Service) -> list[str]:
    cfg = service.cfg
    return [
        "TMDB",
        *(["fanart.tv"] if cfg.fanart_api_key else []),
        *([f"Apple TV ({cfg.regions[0]})"] if cfg.apple_art and cfg.regions else []),
    ]


def alerts(service: Service) -> list[dict[str, Any]]:
    return [{"cause": cause, "since": since} for cause, since in service.worker.notifier.open_alerts()]


def preview_files(folder: Path, target: str = "") -> list[tuple[float, int, str, re.Match[str]]]:
    """(mtime, size, name, match) of each preview in the folder, newest first."""
    found = []
    try:
        paths = list(folder.iterdir())
    except OSError:
        return []
    for path in paths:
        match = PREVIEW_NAME.match(path.name)
        if not match or (target and match["target"] != target):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append((stat.st_mtime, stat.st_size, path.name, match))
    found.sort(key=lambda entry: (-entry[0], entry[2]))
    return found


class Titles:
    """Names for the item being worked on: the stored title, else one server call per key. Status is polled often,
    so the library is never listed here."""

    def __init__(self, service: Service) -> None:
        self.service = service
        self._cache: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def __call__(self, key: str) -> str | None:
        if key.startswith(JOB_PREFIX):
            return None
        if key == LIVE_TV:
            return LIVE_TV_TITLE
        if title := stored_title(self.service.store, key):
            return title
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        try:
            item = self.service.server.item(key)
            title = (str(item.get("title") or "") or None) if item else None
        except (http.RequestError, OSError, ValueError):
            title = None
        with self._lock:
            if len(self._cache) >= TITLE_CACHE:
                self._cache.clear()
            self._cache[key] = title
        return title


def register(ui: "WebUi") -> None:  # noqa: C901, PLR0915
    """Add this module's routes to ui.router."""
    r = ui.router
    service = ui.service
    assert service is not None
    store = service.store
    titles = Titles(service)

    def current() -> dict[str, Any] | None:
        now = service.current
        if now is None:
            return None
        key = str(now["key"])
        label = now.get("label")
        return {
            "key": key, "title": label if key.startswith(JOB_PREFIX) else titles(key), "reason": now.get("reason"),
            "label": label, "started": now.get("started"),
        }  # fmt: skip

    def queue_summary() -> tuple[list[tuple[int, str, str]], dict[str, Any]]:
        snapshot = service.queue_snapshot()
        by_reason = dict(Counter(reason for _, _, reason in snapshot))
        return snapshot, {"total": len(snapshot), "by_reason": by_reason, "held": service.held()}

    def unpaused() -> None:
        if service.paused.is_set():
            raise ApiError(409, PAUSED)

    @r.get("/api/status")
    def status(_req: Request) -> Response:
        cfg = service.cfg
        counts = store.counts()
        next_at = service.next_sweep_at
        paused = service.paused.is_set()
        _, queue = queue_summary()
        return json_response({
            **service.status(),
            "restart_required": ui.restart_required,
            "server": {"name": service.server.name, "url": service.server.url},
            "libraries": list(cfg.libraries),
            "next_sweep_seconds": None if next_at is None or paused else max(0, round(next_at - time.monotonic())),
            "sweep_minutes": cfg.sweep_minutes,
            "daily_at": cfg.daily_at.strftime("%H:%M"),
            "queue": queue,
            "current": current(),
            "images": {status.value: counts.get(status.value, 0) for status in Status},
            "features": features(service),
            "art_sources": art_sources(service),
            "alerts": alerts(service),
        })  # fmt: skip

    @r.post("/api/actions/sweep")
    def sweep(_req: Request) -> Response:
        unpaused()
        service.request_sweep()
        log.info("sweep asked for from the web UI")
        return json_response({"ok": True})

    @r.post("/api/actions/full")
    def full(_req: Request) -> Response:
        unpaused()
        service.request_full()
        log.info("full check asked for from the web UI")
        return json_response({"ok": True})

    @r.post("/api/actions/pause")
    def pause(_req: Request) -> Response:
        if not service.paused.is_set():
            service.pause()
        return json_response({"paused": True})

    @r.post("/api/actions/resume")
    def resume(_req: Request) -> Response:
        if service.paused.is_set():
            service.resume_work()
        return json_response({"paused": False})

    @r.post("/api/actions/test-alert")
    def test_alert(_req: Request) -> Response:
        def run() -> dict[str, Any]:
            notifier = service.worker.notifier
            if not notifier.configured:
                return {"sent": False, "message": "No notification service is set up. Set NOTIFY_URLS."}
            if notifier.send("test", "Alerts from Posteryard reach you."):
                return {"sent": True, "message": "Test alert sent."}
            return {"sent": False, "message": "A notification service did not accept the test alert. See the log."}

        return ui.submit("test-alert", "Send a test alert", run)

    @r.post("/api/actions/restore-all")
    def restore_all(req: Request) -> Response:
        if not service.paused.is_set():
            raise ApiError(409, "Pause Posteryard first, or it would upload the posters again.")
        if req.json().get("confirm") != "RESTORE":
            raise ApiError(400, 'Send "confirm": "RESTORE" to give every image back to the server.')

        def run() -> dict[str, int]:
            restored = service.worker.restore_all()
            log.info("images restored from the web UI", extra=dataclasses.asdict(restored))
            return dataclasses.asdict(restored)

        return ui.submit("restore-all", "Restore every image", run)

    @r.post("/api/actions/restart")
    def restart(_req: Request) -> Response:
        if ui.on_restart is None:
            raise ApiError(409, "Posteryard cannot restart itself here.")
        # A moment later, so this answer goes out before the server stops.
        threading.Timer(RESTART_DELAY, ui.on_restart).start()
        return json_response({"ok": True})

    @r.get("/api/queue")
    def queue(_req: Request) -> Response:
        snapshot, summary = queue_summary()
        items = []
        for priority, key, reason in snapshot[:QUEUE_LIMIT]:
            if key.startswith(JOB_PREFIX):
                job = service.queued_job(key)
                title = job.label if job is not None else None
            else:
                title = LIVE_TV_TITLE if key == LIVE_TV else stored_title(store, key)
            items.append({"key": key, "reason": reason, "priority": priority, "title": title})
        return json_response({**summary, "current": current(), "items": items})

    @r.get("/api/logs")
    def logs(req: Request) -> Response:
        level = req.query.get("level") or "info"
        if level not in ORDER:
            raise ApiError(400, f"level must be one of: {', '.join(ORDER)}")
        after = req.int_arg("after", 0, 0, 10**15)
        limit = req.int_arg("limit", 500, 1, 2000)
        return json_response({"records": ui.logs.since(after, level, limit), "last": ui.logs.last()})

    def status_arg(req: Request) -> str | None:
        status = req.query.get("status") or None
        if status is not None and status not in {s.value for s in Status}:
            raise ApiError(400, f"status must be one of: {', '.join(Status)}")
        return status

    @r.get("/api/activity")
    def activity(req: Request) -> Response:
        status = status_arg(req)
        limit = req.int_arg("limit", 50, 1, 200)
        offset = req.int_arg("offset", 0, 0, 10**9)
        records = store.records(status=status, limit=limit, offset=offset)
        return json_response({"total": store.count(status), "records": [record_json(store, rec) for rec in records]})

    @r.get("/api/problems")
    def problems(_req: Request) -> Response:
        return json_response({
            "failed": [record_json(store, rec) for rec in store.records(status=Status.FAILED, limit=PROBLEM_LIMIT)],
            "manual": [record_json(store, rec) for rec in store.records(status=Status.MANUAL, limit=PROBLEM_LIMIT)],
            "alerts": alerts(service),
        })  # fmt: skip

    @r.post("/api/problems/retry")
    def retry(req: Request) -> Response:
        raw = req.json().get("keys")
        if raw is None:
            keys = [rec.rating_key for rec in store.records(status=Status.FAILED, limit=10**9)]
        elif isinstance(raw, list) and all(isinstance(key, str) and is_item_key(key) for key in raw):
            keys = raw
        else:
            raise ApiError(400, "keys must be a list of rating keys")
        unique = list(dict.fromkeys(keys))
        service.enqueue(unique, "retry")
        log.info("retry asked for from the web UI", extra={"items": len(unique)})
        return json_response({"queued": len(unique)})

    @r.get("/api/previews")
    def previews(req: Request) -> Response:
        target = req.query.get("target", "")
        if target and target not in TARGETS:
            raise ApiError(400, f"target must be one of: {', '.join(TARGETS)}")
        offset = req.int_arg("offset", 0, 0, 10**9)
        limit = req.int_arg("limit", 60, 1, 500)
        found = preview_files(service.cfg.preview_dir, target)
        items = []
        for mtime, size, name, match in found[offset : offset + limit]:
            record = store.get(match["key"], match["target"])
            title = record.title if record and record.title else stored_title(store, match["key"])
            items.append({
                "name": name, "key": match["key"], "target": match["target"], "title": title,
                "mtime": mtime, "size": size, "url": f"/api/previews/{name}",
            })  # fmt: skip
        return json_response({"total": len(found), "items": items})

    @r.get("/api/previews/{name}")
    def preview(req: Request) -> Response:
        name = req.params["name"]
        folder = service.cfg.preview_dir
        path = folder / name
        if not SAFE_NAME.match(name) or path.resolve().parent != folder.resolve() or not path.is_file():
            raise ApiError(404, "no such preview")
        try:
            data = path.read_bytes()
        except OSError:
            raise ApiError(404, "no such preview") from None
        return Response(200, data, "image/jpeg", {"Cache-Control": "no-cache"})

    @r.delete("/api/previews")
    def delete_previews(_req: Request) -> Response:
        deleted = 0
        for _, _, name, _ in preview_files(service.cfg.preview_dir):
            try:
                (service.cfg.preview_dir / name).unlink()
                deleted += 1
            except OSError:
                continue
        log.info("previews deleted from the web UI", extra={"files": deleted})
        return json_response({"deleted": deleted})

    @r.get("/api/guide")
    def guide(_req: Request) -> Response:
        items = [
            {"key": g.key, "title": g.title, "image_key": g.image_key, "picture": g.picture, "seen_at": g.seen_at}
            for g in store.guides()
        ]
        return json_response({"enabled": service.live_tv is not None, "items": items})
