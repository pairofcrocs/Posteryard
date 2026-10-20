import hmac
import itertools
import json
import logging
import platform
import queue
import signal
import threading
import time
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from posteryard import __version__, http, logfmt, memory, overrides, pipeline, service_collections
from posteryard.config import Config
from posteryard.livetv import LiveTv
from posteryard.notify import Event
from posteryard.server import Item, MediaServer, is_item_key
from posteryard.store import Store
from posteryard.webui.app import WebUi
from posteryard.webui.jobs import Job
from posteryard.worker import Outcome, Worker

log = logging.getLogger(__name__)
MAX_BODY = 2 * 1024 * 1024
NEW_EVENTS = frozenset({"library.new"})
JELLYFIN_EVENTS = frozenset({"ItemAdded"})
KINDS = {"movie": ("movie",), "show": ("show", "season", "episode")}
RESTART_LOOKBACK = 6 * 3600
SWEEP_LOOKBACK = 60
WORKER_STALL = 600
TICK = 30
STOP = "stop"
HEARTBEAT_SECONDS = 60
RETRY_SECONDS = 120
URGENT = frozenset({"webhook", "label", "unignored", "ui"})
BACKGROUND = frozenset({"daily", "unlisted"})
NEW_REASONS = frozenset({"webhook", "changed", "retry"})
NEW_BATCH_SECONDS = 900
NEW_WAIT_SECONDS = 300
RESUMED = "resumed after a restart"
LIVE_TV = "live-tv"
JOB_PREFIX = "job:"
RESTART = 3
EVENT_NAMES = {Event.PROBLEMS: "problems", Event.NEW: "new posters", Event.SUMMARY: "daily summary"}


def parse_webhook(content_type: str, body: bytes) -> dict[str, Any] | None:
    """Plex posts multipart/form-data with the event JSON in the `payload` field, Emby in the `data` field. Jellyfin's
    Webhook plugin posts the JSON itself, labelled text/plain. Raises ValueError on bad JSON."""
    raw: bytes | None = None
    if not content_type.startswith("multipart/"):
        raw = body
    else:
        message = BytesParser(policy=policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
        )
        if message.is_multipart():
            for part in message.iter_parts():
                if part.get_param("name", header="content-disposition") in ("payload", "data"):
                    payload = part.get_payload(decode=True)
                    raw = payload if isinstance(payload, bytes) else None
                    break
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except RecursionError:
        raise ValueError("the payload is nested too deeply") from None
    except ValueError:
        if content_type.startswith(("application/json", "multipart/")):
            raise
        return None
    return data if isinstance(data, dict) else None


def other_server(server: MediaServer, store: Store) -> str | None:
    """The data folder records the server it belongs to. Plex and Emby both number items from 1, so records must not
    carry over to another server."""
    current = server.server_id()
    known = store.meta("server")
    if known and known != current:
        return (
            f"The data folder belongs to another media server ({known}), not this {server.name} ({current}). "
            "Use a new data folder, or delete state.db in it to start over."
        )
    if not known:
        store.set_meta("server", current)
    return None


def related_keys(item: object) -> list[str]:
    if not isinstance(item, dict):
        return []
    keys = [item.get("ratingKey"), item.get("parentRatingKey"), item.get("grandparentRatingKey")]
    return [str(k) for k in keys if k and is_item_key(str(k))]


class Service:
    def __init__(
        self, cfg: Config, server: MediaServer, store: Store, worker: Worker, live_tv: LiveTv | None = None
    ) -> None:
        self.cfg, self.server, self.store, self.worker, self.live_tv = cfg, server, store, worker, live_tv
        self.queue: queue.PriorityQueue[tuple[int, int, str, str]] = queue.PriorityQueue()
        self._queued: dict[str, int] = {}
        self._order = itertools.count()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.worker_beat = time.monotonic()
        self.last_sweep_ok = time.monotonic()
        self.threads: list[threading.Thread] = []
        self.exit_code = 0
        self.server_checked = threading.Event()
        self._live_tv_running = threading.Event()
        self._last_heartbeat = float("-inf")
        self._connected = False
        self._tally: Counter[Outcome] = Counter()
        self._full_started: float | None = None
        self._new: list[str] = []
        self._new_poster: bytes | None = None
        self._new_since = 0.0
        self._last_new = float("-inf")
        self.ui: WebUi | None = None
        self._jobs: dict[str, Job] = {}
        self._held: list[tuple[str, str]] = []
        self.current: dict[str, Any] | None = None
        self.next_sweep_at: float | None = None
        self.paused = threading.Event()
        self._wake = threading.Event()
        self._sweep_requested = False
        self._full_requested = False
        if store.meta("paused") == "1":
            self.paused.set()

    def enqueue(self, keys: Iterable[str], reason: str) -> None:
        priority = 0 if reason in URGENT else 2 if reason in BACKGROUND else 1
        for key in keys:
            with self._lock:
                if self._queued.get(key, priority + 1) <= priority:
                    continue
                self._queued[key] = priority
                self.queue.put((priority, next(self._order), key, reason))

    def submit_job(self, job: Job) -> None:
        """Run a web UI job on the worker thread, ahead of library items."""
        key = f"{JOB_PREFIX}{job.id}"
        with self._lock:
            self._jobs[key] = job
        self.enqueue([key], "ui")

    def queued_job(self, key: str) -> Job | None:
        """The web UI job behind a `job:<id>` queue key."""
        with self._lock:
            return self._jobs.get(key)

    def held(self) -> int:
        """Library items taken from the queue while paused. They go back in on resume."""
        with self._lock:
            return len(self._held)

    def queue_snapshot(self) -> list[tuple[int, str, str]]:
        """Waiting work in the order it runs: (priority, key, reason)."""
        with self._lock:
            entries = sorted(self.queue.queue)
            return [(p, key, reason) for p, _, key, reason in entries if self._queued.get(key) == p]

    def pause(self) -> None:
        """Stop sweeps, full passes and library items. Web UI jobs still run."""
        self.store.set_meta("paused", "1")
        self.paused.set()
        log.info("paused from the web UI: no sweeps, full passes or library items until resumed")

    def resume_work(self) -> None:
        self.store.set_meta("paused", "0")
        self.paused.clear()
        with self._lock:
            held, self._held = self._held, []
        for key, reason in held:
            self.enqueue([key], reason)
        log.info("resumed from the web UI", extra={"waiting": len(held)})
        self._wake.set()

    def request_sweep(self) -> None:
        """Have the schedule thread sweep now, so a sweep never runs on two threads at once."""
        self._sweep_requested = True
        self._wake.set()

    def request_full(self) -> None:
        self._full_requested = True
        self._wake.set()

    def request_restart(self) -> None:
        """Stop, and have `serve` start again with the saved settings."""
        log.info("restarting to apply new settings")
        self.exit_code = RESTART
        self.stop()

    def stop(self) -> None:
        """Wake the worker. It must not sleep through a stop: Docker kills the container after 10 seconds."""
        self._stop.set()
        self._wake.set()
        self.queue.put((-1, next(self._order), "", STOP))

    def take(self, timeout: float) -> tuple[str, str]:
        while True:
            priority, _, key, reason = self.queue.get(timeout=timeout)
            if reason == STOP:
                raise queue.Empty
            with self._lock:
                if self._queued.get(key) == priority:
                    del self._queued[key]
                    return key, reason

    def item_added(self, item_id: object) -> None:
        key = str(item_id or "").replace("-", "").lower()
        if not is_item_key(key):
            return
        try:
            item = self.server.item(key)
        except http.RequestError as exc:
            log.warning("webhook item not readable", extra={"key": key, "reason": str(exc)})
            item = None
        self.enqueue(related_keys(dict(item)) if item else [key], "webhook")

    def _work(self) -> None:
        while not self._stop.is_set():
            if not self.server_checked.is_set():
                self.worker_beat = time.monotonic()
                self._stop.wait(1)
                continue
            try:
                key, reason = self.take(TICK)
            except queue.Empty:
                self.worker_beat = time.monotonic()
                if self._stop.is_set():
                    return
                self.idle()
                self.announce_new(idle=True)
                self.worker.rest()
                continue
            if key.startswith(JOB_PREFIX):
                self._run_job(key)
                continue
            if self.paused.is_set():
                with self._lock:
                    self._held.append((key, reason))
                continue
            if key == LIVE_TV:
                self.current = {"key": key, "reason": reason, "started": time.time()}
                self._live_tv()
                self.current = None
                continue
            self._process(key, reason)

    def _process(self, key: str, reason: str) -> None:
        started = time.monotonic()
        self.current = {"key": key, "reason": reason, "started": time.time()}
        try:
            outcome = self.worker.process(key)
        except Exception:
            log.exception("unexpected error", extra={"key": key})
            outcome = Outcome.FAILED
        finally:
            self.current = None
        took = logfmt.took(time.monotonic() - started)
        log.debug("item processed", extra={"key": key, "outcome": outcome, "trigger": reason, "took": took})
        if self._full_started is not None:
            self._tally[outcome] += 1
        if reason in NEW_REASONS:
            self.collect_new(self.worker.fresh)
        self.announce_new(idle=False)
        memory.release()
        self.worker_beat = time.monotonic()

    def _run_job(self, key: str) -> None:
        with self._lock:
            job = self._jobs.pop(key, None)
        if job is None or self.ui is None:
            return
        self.current = {"key": key, "reason": "ui", "label": job.label, "started": time.time()}
        try:
            self.ui.jobs.execute(job)
        finally:
            self.current = None
        memory.release()
        self.worker_beat = time.monotonic()

    def check_live_tv(self) -> None:
        if self.live_tv is None or self._live_tv_running.is_set():
            return
        try:
            due = self.live_tv.due()
        except (http.RequestError, OSError, ValueError) as exc:
            log.warning("live TV guide not checked, trying again at the next sweep", extra={"reason": str(exc)})
            return
        if due:
            self.enqueue([LIVE_TV], "live tv")

    def _urgent_waiting(self) -> bool:
        with self._lock:
            return any(priority == 0 for priority in self._queued.values())

    def _live_tv(self) -> None:
        """A webhook or label change waiting pauses the run. It starts again after them; done shows stay unchanged."""
        if self.live_tv is None:
            return
        paused = False

        def tick() -> bool:
            nonlocal paused
            self.worker_beat = time.monotonic()
            paused = self._urgent_waiting()
            return not (self._stop.is_set() or paused)

        self._live_tv_running.set()
        try:
            self.live_tv.run(tick=tick)
        except (http.RequestError, OSError, ValueError) as exc:
            log.warning("live TV posters not checked, trying again later", extra={"reason": str(exc)})
        except Exception:
            log.exception("unexpected error", extra={"key": LIVE_TV})
        finally:
            self._live_tv_running.clear()
        if paused and not self._stop.is_set():
            self.enqueue([LIVE_TV], "live tv")
        memory.release()
        self.worker_beat = time.monotonic()

    def collect_new(self, fresh: list[tuple[str, bytes]]) -> None:
        if not fresh:
            return
        if not self._new:
            self._new_since = time.monotonic()
        self._new.extend(name for name, _ in fresh)
        self._new_poster = fresh[0][1] if len(self._new) == 1 else None

    def announce_new(self, *, idle: bool) -> None:
        """One message for a group of new posters, at most once per NEW_BATCH_SECONDS."""
        now = time.monotonic()
        if not self._new or now - self._last_new < NEW_BATCH_SECONDS:
            return
        if not idle and now - self._new_since < NEW_WAIT_SECONDS:
            return
        self.worker.notifier.new_posters(self._new, self._new_poster)
        self._new, self._new_poster, self._last_new = [], None, now

    def _kinds(self, section: Item) -> tuple[str, ...]:
        kinds = KINDS[str(section["type"])]
        if self.cfg.collection_posters or self.cfg.service_collections:
            kinds = (*kinds, "collection")
        return kinds

    def _sections(self) -> list[Item]:
        sections = [
            s for s in self.server.sections() if s.get("title") in self.cfg.libraries and s.get("type") in KINDS
        ]
        if not sections:
            raise LookupError(f"{self.server.name} has no movie or TV library named {', '.join(self.cfg.libraries)}")
        return sections

    def _keys(self, items: Iterable[Item]) -> list[str]:
        return [str(i["ratingKey"]) for i in items if self.worker.in_scope(i)]

    def _scoped(self, keys: Iterable[str]) -> list[str]:
        """Keys without their item: those in ONLY_RATING_KEYS, or with a record, which only an item in scope gets."""
        if not (only := self.cfg.only_rating_keys):
            return list(keys)
        known = self.store.keys()
        return [k for k in keys if k in only or k in known]

    def sweep(self, lookback: int = SWEEP_LOOKBACK) -> None:
        started = int(time.time())
        cursor = self.store.meta("sweep_cursor")
        # A new data folder starts the cursor now. The full pass covers the library, and new-poster alerts must not.
        since = int(cursor) - lookback if cursor else started
        for section in self._sections():
            for kind in self._kinds(section):
                changed = self.server.changed_since(str(section["key"]), kind, since)
                self.enqueue(self._keys(changed), "changed")
                for label in (overrides.CUSTOM_LABEL, overrides.NEXT_LABEL):
                    # Adding a label does not change an item's updatedAt, so changed_since misses it.
                    labelled = self.server.section_items(str(section["key"]), kind, label=label)
                    self.enqueue(self._keys(labelled), "label")
        ignored = {
            str(i["ratingKey"])
            for section in self._sections()
            for kind in self._kinds(section)
            for i in self.server.section_items(str(section["key"]), kind, label=overrides.IGNORE_LABEL)
        }
        # A removed label leaves updatedAt untouched too, so items that lost the ignore label are found here.
        # Their old records are dropped: a poster chosen while ignored must not count as a manual change.
        released = sorted(set(json.loads(self.store.meta("ignored_keys", "[]"))) - ignored)
        for key in released:
            self.store.forget(key)
        self.enqueue(self._scoped(released), "unignored")
        self.store.set_meta("ignored_keys", json.dumps(sorted(ignored)))
        previous: dict[str, str] = json.loads(self.store.meta("leaving_dates", "{}"))
        current = {key: day.isoformat() for key, day in self.worker.leaving_days().items()}
        today = date.today().isoformat()
        new_day = self.store.meta("leaving_checked") != today
        leaving = previous.keys() | current.keys()
        self.enqueue(
            self._scoped(sorted(k for k in leaving if new_day or previous.get(k) != current.get(k))), "leaving"
        )
        self.enqueue(self._scoped(self.store.retry_due()), "retry")
        self.store.set_meta("leaving_dates", json.dumps(current, sort_keys=True))
        self.store.set_meta("leaving_checked", today)
        self.store.set_meta("sweep_cursor", str(started))
        self.last_sweep_ok = time.monotonic()

    def idle(self) -> None:
        with self._lock:
            busy = bool(self._queued)
        if not busy and self.store.meta("full_pending") == "1":
            self.store.set_meta("full_pending", "0")
            self.report_full()

    def report_full(self) -> None:
        tally, started = self._tally, self._full_started
        self._tally, self._full_started = Counter(), None
        took = logfmt.took(time.monotonic() - started) if started is not None else None
        changed = tally[Outcome.PREVIEW] if self.cfg.dry_run else tally[Outcome.UPLOADED]
        log.info("full check done", extra={
            "took": took, "previews" if self.cfg.dry_run else "uploaded": changed,
            "unchanged": tally[Outcome.UNCHANGED], "by_hand": tally[Outcome.MANUAL], "skipped": tally[Outcome.SKIPPED],
            "removed": tally[Outcome.GONE], "failed": tally[Outcome.FAILED],
        })  # fmt: skip
        if self.store.meta("full_reason") == "daily" and (changed or tally[Outcome.FAILED]):
            parts = [f"{changed:,} {'previewed' if self.cfg.dry_run else 'updated'}"]
            if tally[Outcome.FAILED]:
                parts.append(f"{tally[Outcome.FAILED]:,} failed")
            checked = f"Checked {sum(tally.values()):,} items" + (f" in {took}" if took else "")
            self.worker.notifier.summary(f"{checked}: {', '.join(parts)}.", failed=bool(tally[Outcome.FAILED]))

    def resume(self) -> None:
        if self.store.meta("full_pending") == "1":
            self.full(RESUMED)

    def _sync_collections(self) -> None:
        shows = [s for s in self._sections() if s.get("type") == "show"]
        if self.cfg.dry_run:
            log.info("service collections not changed while DRY_RUN is on")
            return
        if not self.server.collections_per_library and len(shows) > 1:
            log.warning("service collections use one library only", extra={
                "server": self.server.name, "library": shows[0].get("title"),
                "reason": f"{self.server.name} collections belong to no library",
            })  # fmt: skip
            shows = shows[:1]
        failed = False
        for section in shows:
            try:
                service_collections.sync(self.server, self.worker.ctx, str(section["key"]))
            except (http.RequestError, ValueError) as exc:
                failed = True
                log.warning("service collections failed", extra={"library": section.get("title"), "reason": str(exc)})
                self.worker.notifier.alert("collections", f"Service collections could not be updated: {exc}")
        if not failed:
            self.worker.notifier.resolve("collections", "Service collections update again.")

    def full(self, reason: str) -> None:
        listed: set[str] = set()
        seen: set[str] = set()
        if self.cfg.service_collections:
            self._sync_collections()
        for section in self._sections():
            for kind in self._kinds(section):
                items = list(self.server.section_items(str(section["key"]), kind))
                listed.update(str(i["ratingKey"]) for i in items)
                seen.update(self._keys(items))
        unlisted = sorted(self.store.keys() - listed)
        self.enqueue(sorted(seen), "daily")
        self.enqueue(unlisted, "unlisted")
        self.store.set_meta("full_pending", "1")
        if reason != RESUMED:
            self.store.set_meta("full_reason", reason)
        self.store.set_meta("last_full", datetime.now().date().isoformat())
        self._tally, self._full_started = Counter(), time.monotonic()
        log.info("full check started", extra={"reason": reason, "items": len(seen), "missing": len(unlisted)})

    def settings_signature(self) -> str:
        cfg = self.cfg
        return json.dumps(
            {
                "version": __version__,
                "dry_run": cfg.dry_run,
                "only": sorted(cfg.only_rating_keys),
                "libraries": cfg.libraries,
                "regions": cfg.regions,
                "quality": [cfg.quality.video, cfg.quality.hdr, cfg.quality.audio],
                "status_labels": cfg.status_labels,
                "accessibility": sorted(cfg.accessibility),
                "episodes": cfg.episodes,
                "logo_languages": cfg.logo_languages,
                "prefer_wordmark": cfg.prefer_wordmark,
                "collection_posters": cfg.collection_posters,
                "service_collections": cfg.service_collections,
                "fanart": bool(cfg.fanart_api_key),
                "apple_art": cfg.apple_art,
                "text_check": cfg.text_check,
            },
            sort_keys=True,
        )

    def _rest(self) -> None:
        """Wait a tick, or less when a stop or a web UI request comes."""
        self._wake.wait(TICK)
        self._wake.clear()

    def _schedule(self) -> None:  # noqa: C901, PLR0912, PLR0915
        next_sweep = 0.0
        lookback = RESTART_LOOKBACK
        resumed = False
        retry_at = 0.0
        self.worker.notifier.resolve("restart", "Posteryard is running again.")
        while not self._stop.is_set():
            requested = self._sweep_requested or self._full_requested
            if time.monotonic() < retry_at and not requested:
                self.heartbeat()
                self._rest()
                continue
            try:
                if not self.server_checked.is_set():
                    if problem := other_server(self.server, self.store):
                        log.error("data folder belongs to another server", extra={"reason": problem})
                        self.worker.notifier.alert("server", problem)
                        self.exit_code = 2
                        self.stop()
                        return
                    self.server_checked.set()
                if not self._connected:
                    self.connect()
                if self.paused.is_set():
                    # A pause is no stall: health stays green while nothing is scheduled.
                    self.last_sweep_ok = time.monotonic()
                    self.heartbeat()
                    self._rest()
                    continue
                signature = self.settings_signature()
                if not resumed:
                    if self.store.meta("settings_signature") == signature:
                        self.resume()
                    resumed = True
                if self.store.meta("settings_signature") != signature:
                    self.full("version or settings changed" if self.store.meta("settings_signature") else "first run")
                    self.store.set_meta("settings_signature", signature)
                now = datetime.now()
                last_full = self.store.meta("last_full")
                if not last_full or (last_full != now.date().isoformat() and now.time() >= self.cfg.daily_at):
                    self.full("daily")
                if self._full_requested:
                    self._full_requested = False
                    self.full("web UI")
                if self._sweep_requested or time.monotonic() >= next_sweep:
                    self._sweep_requested = False
                    self.sweep(lookback)
                    lookback = SWEEP_LOOKBACK
                    next_sweep = time.monotonic() + self.cfg.sweep_minutes * 60
                    self.next_sweep_at = next_sweep
                    self.check_live_tv()
                self.worker.notifier.resolve("schedule", "Scheduled runs work again.")
            except (http.RequestError, OSError, ValueError, LookupError) as exc:
                log.warning("scheduled run failed, trying again in 2 minutes", extra={"reason": str(exc)})
                self.worker.notifier.alert("schedule", f"A scheduled run failed: {exc}")
                retry_at = time.monotonic() + RETRY_SECONDS
            except Exception as exc:  # The schedule thread must survive any error.
                log.exception("scheduled run failed, trying again in 2 minutes")
                self.worker.notifier.alert("schedule", f"A scheduled run failed: {type(exc).__name__}: {exc}")
                retry_at = time.monotonic() + RETRY_SECONDS
            self.heartbeat()
            self._rest()

    def connect(self) -> None:
        found = [str(s.get("title")) for s in self._sections()]
        log.info("media server connected", extra={"server": self.server.name, "libraries": ", ".join(found)})
        for missing in [name for name in self.cfg.libraries if name not in found]:
            log.warning("library not found", extra={"library": missing, "server": self.server.name})
        self._connected = True

    def heartbeat(self) -> None:
        now = time.monotonic()
        if not self.cfg.heartbeat_url or now - self._last_heartbeat < HEARTBEAT_SECONDS or not self.healthy():
            return
        self._last_heartbeat = now
        try:
            http.request("GET", self.cfg.heartbeat_url, timeout=10, retries=1)
        except http.HttpError as exc:
            log.warning("HEARTBEAT_URL refused the call", extra={"reason": f"HTTP {exc.status}"})
        except http.RequestError:
            log.warning("HEARTBEAT_URL not reachable")

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        sweep_limit = max(3 * self.cfg.sweep_minutes * 60, 900)
        checks = {
            "threads_running": all(thread.is_alive() for thread in self.threads),
            "worker_responsive": now - self.worker_beat < WORKER_STALL,
            "sweep_recent": now - self.last_sweep_ok < sweep_limit,
        }
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "last_sweep_seconds_ago": round(now - self.last_sweep_ok),
            "last_full_pass": self.store.meta("last_full") or None,
            "full_pass_running": self.store.meta("full_pending") == "1",
            "queue": len(self._queued),
            "images": self.store.counts(),
            "dry_run": self.cfg.dry_run,
            "paused": self.paused.is_set(),
            "version": __version__,
        }

    def healthy(self) -> bool:
        return bool(self.status()["ok"])

    def _watch(self, server: ThreadingHTTPServer) -> None:
        while not self._stop.wait(TICK):
            dead = [thread.name for thread in self.threads if not thread.is_alive()]
            if dead:
                log.error(
                    "service thread stopped, exiting so the container restarts", extra={"threads": ", ".join(dead)}
                )
                self.worker.notifier.alert("restart", f"Posteryard restarts because {', '.join(dead)} stopped.")
                self.exit_code = 1
                self.stop()
        server.shutdown()

    def handler(self) -> type[BaseHTTPRequestHandler]:  # noqa: C901
        service = self
        webhook_path = f"/webhook/{self.cfg.webhook_secret}"

        class Handler(BaseHTTPRequestHandler):
            timeout = 10

            def log_message(self, format: str, *args: Any) -> None:
                """Silent: the request line holds the webhook secret."""

            def _reply(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _ui(self, method: str) -> None:
                if service.ui is None:
                    self._reply(404, {"error": "not found"})
                else:
                    service.ui.handle(self, method)

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                if path == "/healthz":
                    status = service.status()
                    self._reply(200 if status["ok"] else 503, status)
                else:
                    self._ui("GET")

            def do_HEAD(self) -> None:
                if self.path.split("?")[0] != "/healthz":
                    self._ui("HEAD")
                    return
                self.send_response(200 if service.healthy() else 503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_PUT(self) -> None:
                self._ui("PUT")

            def do_DELETE(self) -> None:
                self._ui("DELETE")

            def do_POST(self) -> None:
                if not hmac.compare_digest(self.path.split("?")[0].encode(), webhook_path.encode()):
                    self._ui("POST")
                    return
                raw_length = self.headers.get("Content-Length") or "0"
                length = int(raw_length) if raw_length.isascii() and raw_length.isdigit() else 0
                if not 0 < length <= MAX_BODY:
                    self._reply(413, {"error": "bad size"})
                    return
                try:
                    payload = parse_webhook(self.headers.get("Content-Type", ""), self.rfile.read(length))
                except ValueError:
                    self._reply(400, {"error": "bad payload"})
                    return
                if payload and payload.get("event") in NEW_EVENTS and payload.get("Metadata"):
                    service.enqueue(related_keys(payload["Metadata"]), "webhook")
                elif payload and payload.get("NotificationType") in JELLYFIN_EVENTS:
                    service.item_added(payload.get("ItemId"))
                elif payload and payload.get("Event") in NEW_EVENTS and isinstance(payload.get("Item"), dict):
                    service.item_added(payload["Item"].get("Id"))
                self._reply(200, {"ok": True})

        return Handler

    def banner(self) -> str:
        cfg = self.cfg
        art = [
            "TMDB",
            *(["fanart.tv"] if cfg.fanart_api_key else []),
            *([f"Apple TV ({cfg.regions[0]})"] if cfg.apple_art and cfg.regions else []),
        ]
        extras = [
            *(["status labels"] if cfg.status_labels else []),
            *(["leaving labels from Maintainerr"] if cfg.maintainerr_url else []),
            *(["collection posters"] if cfg.collection_posters else []),
            *(["service collections"] if cfg.service_collections else []),
            *(["Live TV posters"] if self.live_tv is not None else []),
        ]
        services = len(cfg.notify_urls)
        alerts = (
            f"{services} service{'s' if services != 1 else ''}: "
            + ", ".join(EVENT_NAMES[e] for e in Event if e in cfg.notify_events)
            if services
            else "off, NOTIFY_URLS is empty"
        )
        rows = [
            ("Server", f"{self.server.name}, libraries {', '.join(cfg.libraries)}"),
            ("Art", ", ".join(art)),
            ("Extras", ", ".join(extras) or "none"),
            ("Schedule", f"new items every {cfg.sweep_minutes} min, full check daily at {cfg.daily_at:%H:%M}"),
            ("Alerts", alerts),
            ("Heartbeat", "on" if cfg.heartbeat_url else "off"),
            ("Webhook", f":{cfg.listen_port} in the container"),
            ("Web UI", f"http://SERVER-IP:{cfg.listen_port}" if cfg.web_ui else "off, WEB_UI=false"),
            ("Data", str(cfg.data_dir)),
        ]
        if cfg.only_rating_keys:
            rows.append(("Only", f"{len(cfg.only_rating_keys)} items from ONLY_RATING_KEYS"))
        if not cfg.text_check:
            rows.append(("Text check", "off, art is not checked for printed titles"))
        if cfg.dry_run:
            rows.append(("Mode", "DRY_RUN, previews only, nothing is uploaded"))
        return logfmt.banner(f"Posteryard {__version__} · Python {platform.python_version()}", rows)

    def drop_old_choices(self) -> None:
        dropped = sum(self.store.drop_choices(family, keep) for family, keep in pipeline.CHOICE_FAMILIES.items())
        if dropped:
            log.info("old cached choices deleted", extra={"rows": dropped})

    def attach_ui(self) -> None:
        if self.cfg.web_ui and self.ui is None:
            password = self.cfg.ui_password or self.cfg.webhook_secret
            self.ui = WebUi(self.cfg.data_dir, password, cfg=self.cfg, service=self)

    def run(self) -> int:
        self.attach_ui()
        print(self.banner(), flush=True)
        self.drop_old_choices()
        log.info(
            "service ready", extra={"version": __version__, "port": self.cfg.listen_port, "dry_run": self.cfg.dry_run}
        )
        if self.cfg.dry_run:
            log.warning("DRY_RUN is on: posters are saved as previews and not uploaded. Set DRY_RUN=false to upload")
        self.threads = [
            threading.Thread(target=self._work, daemon=True, name="worker"),
            threading.Thread(target=self._schedule, daemon=True, name="schedule"),
        ]
        for thread in self.threads:
            thread.start()
        server = ThreadingHTTPServer(("0.0.0.0", self.cfg.listen_port), self.handler())
        server.daemon_threads = True

        def stop(signum: int, _frame: object) -> None:
            log.info("shutting down", extra={"signal": signal.Signals(signum).name})
            self.stop()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        threading.Thread(target=self._watch, args=(server,), daemon=True, name="watch").start()
        server.serve_forever()
        server.server_close()
        for thread in self.threads:
            thread.join(timeout=20)
        return self.exit_code
