"""Web UI API: the library and its titles. See docs/webui-api.md."""

import io
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from PIL import Image, JpegImagePlugin

from posteryard import apple, http, lookup, memory, overrides, pipeline, why
from posteryard.fanart import is_fanart
from posteryard.jellyfin import Emby, Jellyfin
from posteryard.plex import Plex
from posteryard.server import TARGETS, Item, MediaServer, Target, is_item_key, labels, tmdb_id
from posteryard.store import Status, Store
from posteryard.tmdb import IMAGES, Kind, open_image
from posteryard.webui.jobs import JobError
from posteryard.webui.routing import ApiError, Request, Response, json_response
from posteryard.worker import ITEM_TARGET, Outcome, Worker

if TYPE_CHECKING:
    from posteryard.webui.app import WebUi

LISTING_SECONDS = 60
LISTINGS_KEPT = 64
RENDERS_KEPT = 100
IMAGE_CACHE_BYTES = 48 * 1024 * 1024
IMAGE_CACHE_COUNT = 600
RENDER_CACHE_BYTES = 96 * 1024 * 1024
RENDER_QUALITY = 88
THUMB_QUALITY = 82
SEARCH_LIMIT = 12
PAGE_LIMIT = 200
MIN_WIDTH, MAX_WIDTH = 16, 2000
MAX_SEASONS = 60
LIBRARY_KINDS = ("movie", "show")
KINDS = (*LIBRARY_KINDS, "collection")
SORTS = ("title", "added", "updated")
STATUSES = frozenset({*(status.value for status in Status), "none"})
OVERRIDES = frozenset({"custom", "skip"})
TARGET_NOUNS = {"poster": "poster", "art": "background", "thumb": "thumbnail"}
SOURCES = ("auto", "preview", "server")
LABEL_PREFIX = "posteryard-"
IMAGE_TYPES = {"movie": ["poster", "art"], "show": ["poster", "art"], "season": ["poster"], "episode": ["thumb"]}
TMDB_PATH = re.compile(r"^/[\w-]+(\.[\w-]+)*$")
RENDER_ID = re.compile(r"^([0-9a-f]{16})\.jpg$")
IMAGE_HEADERS = {"Cache-Control": "private, max-age=60"}
JPEG = "image/jpeg"


class TtlCache:
    """Values from the media server, kept `seconds` and at most `size` of them."""

    def __init__(self, seconds: float, size: int) -> None:
        self.seconds, self.size = seconds, size
        self._entries: OrderedDict[tuple[str, ...], tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, ...], load: Callable[[], Any], *, refresh: bool = False) -> Any:
        with self._lock:
            hit = self._entries.get(key)
            if hit and not refresh and time.monotonic() - hit[0] < self.seconds:
                self._entries.move_to_end(key)
                return hit[1]
        value = load()
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.size:
                self._entries.popitem(last=False)
        return value


class BlobCache:
    """JPEGs kept in memory, oldest dropped first past `max_bytes` or `max_count`."""

    def __init__(self, max_bytes: int, max_count: int) -> None:
        self.max_bytes, self.max_count = max_bytes, max_count
        self._blobs: OrderedDict[tuple[str, ...], bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[str, ...]) -> bytes | None:
        with self._lock:
            data = self._blobs.get(key)
            if data is not None:
                self._blobs.move_to_end(key)
            return data

    def put(self, key: tuple[str, ...], data: bytes) -> None:
        with self._lock:
            old = self._blobs.pop(key, None)
            self._size += len(data) - (len(old) if old is not None else 0)
            self._blobs[key] = data
            while self._blobs and (self._size > self.max_bytes or len(self._blobs) > self.max_count):
                self._size -= len(self._blobs.popitem(last=False)[1])

    def drop(self, first: str) -> None:
        """Forget every entry whose key starts with `first`."""
        with self._lock:
            for key in [k for k in self._blobs if k[0] == first]:
                self._size -= len(self._blobs.pop(key))


def _norm(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _raw(item: Item) -> Mapping[str, Any]:
    """The item with the fields Plex sends beyond `Item`, such as parentTitle and childCount."""
    return cast(Mapping[str, Any], item)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def base_summary(item: Item, library: str = "", library_key: str = "") -> dict[str, Any]:
    """An ItemSummary without the parts read from the store."""
    parent = item.get("parentRatingKey")
    added = _int_or_none(item.get("addedAt"))
    return {
        "key": str(item["ratingKey"]),
        "title": str(item.get("title", "")),
        "year": _int_or_none(item.get("year")) or None,
        "type": str(item.get("type", "")),
        "library": library or str(item.get("librarySectionTitle") or "") or None,
        "library_key": library_key or str(item.get("librarySectionID") or "") or None,
        "added_at": added,
        "updated_at": _int_or_none(item.get("updatedAt")) or added,
        "labels": sorted(tag for tag in labels(item) if tag.startswith(LABEL_PREFIX)),
        "parent_key": str(parent) if parent else None,
        "index": _int_or_none(item.get("index")),
    }


def image_url(path: str | None, size: str) -> str | None:
    """A browser address for a Why path. Custom art (`file:`) has none."""
    if not path:
        return None
    if path.startswith("/") and not path.startswith("//"):
        return f"{IMAGES}/{size}{path}"
    return path if path.startswith("https://") else None


def why_json(result: why.Report) -> dict[str, Any]:
    groups = [
        {
            "name": name,
            "candidates": [
                {
                    "path": c.ref.path,
                    "thumb_url": image_url(c.ref.path, "w342"),
                    "full_url": image_url(c.ref.path, "original"),
                    "width": c.ref.width,
                    "height": c.ref.height,
                    "votes": why.votes(c.ref),
                    "verdict": why.verdict(result, c),
                    "used": c.ref.path == result.art,
                    "skipped": c.ref.path in result.skipped,
                    "clean": c.reason is None,
                }
                for c in candidates
            ],
        }
        for name, candidates in result.groups
    ]
    return {
        "art": result.art, "art_url": image_url(result.art, "w500"), "note": result.note,
        "logo": result.logo, "logo_url": image_url(result.logo, "w500"),
        "notices": result.notices, "unchecked": result.unchecked, "groups": groups,
    }  # fmt: skip


def server_url(server: MediaServer, key: str) -> str | None:
    """Where the title opens in the server's own app, when that can be told without guessing."""
    if isinstance(server, Plex):
        try:
            machine = server.machine_id()
        except (http.RequestError, KeyError, ValueError):
            return None
        return (
            f"https://app.plex.tv/desktop/#!/server/{quote(machine, safe='')}/details"
            f"?key={quote(f'/library/metadata/{key}', safe='')}"
        )
    if isinstance(server, Emby):
        return f"{server.url.rstrip('/')}/web/index.html#!/item?id={key}"
    if isinstance(server, Jellyfin):
        return f"{server.url.rstrip('/')}/web/index.html#!/details?id={key}"
    return None


def resize_jpeg(data: bytes, width: int, *, trusted: bool) -> bytes:
    """The image as a JPEG `width` pixels wide at most, keeping its shape. 0 keeps the width."""
    if not width and data[:2] == b"\xff\xd8":
        return data
    image: Image.Image
    if trusted:
        image = Image.open(io.BytesIO(data))
        if width and isinstance(image, JpegImagePlugin.JpegImageFile):
            image.draft("RGB", (width, max(1, image.height * width // image.width)))
    else:
        image = open_image(data)
    if width and image.width > width:
        height = max(1, round(image.height * width / image.width))
        image = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    return _jpeg(image, THUMB_QUALITY)


def _jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


@contextmanager
def _server_errors() -> Iterator[None]:
    try:
        yield
    except http.RequestError as exc:
        raise ApiError(502, str(exc)) from None


@contextmanager
def _job_errors() -> Iterator[None]:
    try:
        yield
    except (pipeline.NotFoundError, overrides.ArtError, lookup.TitleError, http.RequestError) as exc:
        raise JobError(str(exc)) from None
    except (ValueError, OSError) as exc:
        raise JobError(f"{type(exc).__name__}: {exc}") from None


def _rating_key(req: Request) -> str:
    key = req.params["key"]
    if not is_item_key(key):
        raise ApiError(400, "not a valid item key")
    return key


def _choice(req: Request, name: str, allowed: tuple[str, ...] | frozenset[str], default: str = "") -> str:
    value = req.query.get(name, "") or default
    if value and value not in allowed:
        raise ApiError(400, f"{name} must be one of {', '.join(sorted(allowed))}")
    return value


def _label(item: Item | None, key: str) -> str:
    if item is None:
        return key
    raw = _raw(item)
    show = raw.get("grandparentTitle") if item.get("type") == "episode" else raw.get("parentTitle")
    title = str(item.get("title") or key)
    title = f"{title} ({item['year']})" if item.get("year") and item.get("type") in KINDS else title
    return f"{show} · {title}" if show and item.get("type") in ("season", "episode") else title


class ItemsApi:
    """The routes of docs/webui-api.md "Library and titles"."""

    def __init__(self, ui: "WebUi") -> None:
        assert ui.service is not None and ui.cfg is not None
        self.ui, self.service, self.cfg = ui, ui.service, ui.cfg
        self.listings = TtlCache(LISTING_SECONDS, LISTINGS_KEPT)
        self.images = BlobCache(IMAGE_CACHE_BYTES, IMAGE_CACHE_COUNT)
        self.renders = BlobCache(RENDER_CACHE_BYTES, RENDERS_KEPT)

    @property
    def server(self) -> MediaServer:
        return self.service.server

    @property
    def store(self) -> Store:
        return self.service.store

    @property
    def worker(self) -> Worker:
        return self.service.worker

    # Listing

    def sections(self, *, refresh: bool = False) -> list[Item]:
        def load() -> list[Item]:
            with _server_errors():
                return [s for s in self.server.sections() if s.get("type") in LIBRARY_KINDS]

        found: list[Item] = self.listings.get(("sections",), load, refresh=refresh)
        return found

    def listing(self, section: Item, kind: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        key, title = str(section["key"]), str(section.get("title", ""))

        def load() -> list[dict[str, Any]]:
            with _server_errors():
                return [base_summary(item, title, key) for item in self.server.section_items(key, kind)]

        found: list[dict[str, Any]] = self.listings.get(("items", key, kind), load, refresh=refresh)
        return found

    def titles(self, library: str = "", kind: str = "", *, refresh: bool = False) -> list[dict[str, Any]]:
        """Every title of one library, or of every managed library, without duplicates."""
        sections = self.sections(refresh=refresh)
        if library:
            chosen = [s for s in sections if library in (str(s.get("key")), s.get("title"))]
            if not chosen:
                raise ApiError(404, f"{self.server.name} has no movie or TV library {library}")
        else:
            chosen = [s for s in sections if s.get("title") in self.cfg.libraries]
        seen: set[str] = set()
        found: list[dict[str, Any]] = []
        for section in chosen:
            wanted = kind or str(section.get("type"))
            if wanted != "collection" and wanted != section.get("type"):
                continue
            for entry in self.listing(section, wanted, refresh=refresh):
                if entry["key"] not in seen:
                    seen.add(entry["key"])
                    found.append(entry)
        return found

    def summaries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Entries with their status and override from the store."""
        posters = self.store.statuses("poster")
        thumbs = self.store.statuses("thumb") if any(e["type"] == "episode" for e in entries) else {}
        found = self.store.all_overrides()
        out = []
        for entry in entries:
            override = found.get(entry["key"])
            kind = "custom" if override and override.custom else "skip" if override and override.skip else None
            status = (thumbs if entry["type"] == "episode" else posters).get(entry["key"])
            out.append({**entry, "status": status, "override": kind})
        return out

    def libraries(self, req: Request) -> Response:
        refresh = req.query.get("refresh") == "1"
        out = [
            {
                "key": str(s["key"]),
                "title": str(s.get("title", "")),
                "type": str(s.get("type")),
                "managed": s.get("title") in self.cfg.libraries,
                "count": len(self.listing(s, str(s.get("type")), refresh=refresh)),
            }
            for s in self.sections(refresh=refresh)
        ]
        return json_response({"libraries": out})

    def items(self, req: Request) -> Response:
        kind = _choice(req, "kind", KINDS)
        sort = _choice(req, "sort", SORTS, "title")
        status = _choice(req, "status", STATUSES)
        override = _choice(req, "override", OVERRIDES)
        offset, limit = req.int_arg("offset", 0, 0, 10_000_000), req.int_arg("limit", 60, 1, PAGE_LIMIT)
        entries = self.titles(req.query.get("library", ""), kind, refresh=req.query.get("refresh") == "1")
        if query := _norm(req.query.get("q", "")):
            entries = [e for e in entries if query in _norm(e["title"])]
        found = self.summaries(entries)
        if status:
            found = [s for s in found if s["status"] == (None if status == "none" else status)]
        if override:
            found = [s for s in found if s["override"] == override]
        found.sort(key=lambda s: (s["title"].casefold(), s["year"] or 0))
        if sort != "title":
            field = "added_at" if sort == "added" else "updated_at"
            found.sort(key=lambda s: s[field] or 0, reverse=True)
        return json_response({"total": len(found), "items": found[offset : offset + limit]})

    def search(self, req: Request) -> Response:
        entries = {e["key"]: e for e in self.titles(refresh=req.query.get("refresh") == "1")}
        matches = [lookup.Match(e["key"], e["title"], e["year"], e["type"]) for e in entries.values()]
        best = lookup.search(matches, req.query.get("q", ""))[:SEARCH_LIMIT]
        return json_response({"items": self.summaries([entries[m.rating_key] for m in best])})

    # One title

    def item(self, key: str) -> Item:
        with _server_errors():
            found = self.server.item(key)
        if found is None:
            raise ApiError(404, f"{self.server.name} has no item {key}")
        return found

    def _relative(self, key: Any, title: Any) -> dict[str, str] | None:
        if not key:
            return None
        if not title:
            with _server_errors():
                found = self.server.item(str(key))
            title = found.get("title", "") if found else ""
        return {"key": str(key), "title": str(title)}

    def _children_count(self, item: Item) -> int:
        raw, kind, key = _raw(item), item.get("type"), str(item["ratingKey"])
        counted = {"show": "childCount", "season": "leafCount", "collection": "childCount"}.get(str(kind))
        if counted is None:
            return 0
        if (count := _int_or_none(raw.get(counted))) is not None:
            return count
        with _server_errors():
            return len(self.server.collection_children(key) if kind == "collection" else self.server.children(key))

    def detail(self, req: Request) -> Response:
        key = _rating_key(req)
        item = self.item(key)
        raw, kind = _raw(item), str(item.get("type", ""))
        summary = self.summaries([base_summary(item)])[0]
        records = self.store.records(rating_key=key, limit=20)
        failed = next((r for r in records if r.target == ITEM_TARGET), None)
        override = self.store.override(key)
        targets = IMAGE_TYPES.get(kind, ["poster"])
        return json_response({
            **summary,
            "parent": self._relative(item.get("parentRatingKey"), raw.get("parentTitle")),
            "grandparent": self._relative(item.get("grandparentRatingKey"), raw.get("grandparentTitle")),
            "tmdb_id": tmdb_id(item),
            "server_url": server_url(self.server, key),
            "records": [
                {"target": r.target, "status": r.status, "updated_at": r.updated_at, "failures": r.failures,
                 "last_error": self.store.last_error(key, r.target), "fingerprint": r.fingerprint,
                 "image_key": r.image_key}
                for r in records if r.target != ITEM_TARGET
            ],
            "item_error": {
                "failures": failed.failures, "last_error": self.store.last_error(key, ITEM_TARGET),
                "updated_at": failed.updated_at,
            } if failed else None,
            "override": {
                "custom": bool(override.custom), "source": override.source, "skip": sorted(override.skip)
            } if override else None,
            "ignored": overrides.IGNORE_LABEL in labels(item),
            "all_labels": [str(label.get("tag", "")) for label in item.get("Label") or []],
            "targets": targets,
            "has_preview": {t: self._preview(key, t).is_file() for t in targets},
            "children_count": self._children_count(item),
        })  # fmt: skip

    def children(self, req: Request) -> Response:
        key = _rating_key(req)
        parent = self.item(key)
        with _server_errors():
            found = self.server.children(key)
        library, library_key = str(parent.get("librarySectionTitle") or ""), str(parent.get("librarySectionID") or "")
        return json_response({"items": self.summaries([base_summary(c, library, library_key) for c in found])})

    # Images

    def _preview(self, key: str, target: str) -> Path:
        return self.cfg.preview_dir / f"{key}-{target}.jpg"

    def image(self, req: Request) -> Response:
        key = _rating_key(req)
        target = _choice(req, "target", tuple(sorted(TARGETS)), "poster")
        source = _choice(req, "source", SOURCES, "auto")
        width = req.int_arg("w", 0, MIN_WIDTH, MAX_WIDTH)
        if source in ("auto", "preview"):
            path = self._preview(key, target)
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                stamp = None
            if stamp is not None:
                return Response(200, self._preview_jpeg(path, key, target, width, stamp), JPEG, IMAGE_HEADERS)
            if source == "preview":
                raise ApiError(404, "there is no saved preview of this image")
        return Response(200, self._server_jpeg(key, target, width), JPEG, IMAGE_HEADERS)

    def _preview_jpeg(self, path: Path, key: str, target: str, width: int, stamp: int) -> bytes:
        if not width:
            return path.read_bytes()
        cache_key = (key, target, "preview", str(width), str(stamp))
        if (hit := self.images.get(cache_key)) is not None:
            return hit
        data = resize_jpeg(path.read_bytes(), width, trusted=True)
        self.images.put(cache_key, data)
        return data

    def _server_jpeg(self, key: str, target: str, width: int) -> bytes:
        # A server image is kept for the current minute only: it changes when Posteryard uploads.
        cache_key = (key, target, "server", str(width), str(int(time.time() // LISTING_SECONDS)))
        if (hit := self.images.get(cache_key)) is not None:
            return hit
        item = self.item(key)
        noun = TARGET_NOUNS[target]
        try:
            data = self.server.image_bytes(item, cast(Target, target))
        except http.HttpError as exc:
            if exc.status == 404:
                raise ApiError(404, f"{self.server.name} has no {noun} for this item") from None
            raise ApiError(502, str(exc)) from None
        except http.RequestError as exc:
            raise ApiError(502, str(exc)) from None
        if not data:
            raise ApiError(404, f"{self.server.name} has no {noun} for this item")
        try:
            data = resize_jpeg(data, width, trusted=False)
        except (ValueError, OSError) as exc:
            raise ApiError(502, f"{self.server.name} sent a {noun} that could not be read: {exc}") from None
        self.images.put(cache_key, data)
        return data

    def render_image(self, req: Request) -> Response:
        found = RENDER_ID.match(req.params["name"])
        data = self.renders.get((found.group(1),)) if found else None
        if data is None:
            raise ApiError(404, "that render is gone. Render again.")
        return Response(200, data, JPEG, {"Cache-Control": "private, max-age=3600"})

    # Jobs: they run on the worker thread, between library items.

    def _render_plans(self, plans: list[pipeline.Plan]) -> dict[str, Any]:
        renders = []
        for plan in plans:
            record = self.store.get(plan.rating_key, plan.target)
            render_id = secrets.token_hex(8)
            self.renders.put((render_id,), _jpeg(plan.draw(), RENDER_QUALITY))
            renders.append({
                "target": plan.target, "name": plan.name, "notes": plan.notes, "fingerprint": plan.fingerprint,
                "changed": record is None or record.fingerprint != plan.fingerprint,
                "url": f"/api/renders/{render_id}.jpg",
            })  # fmt: skip
        memory.release()
        return {"renders": renders}

    def _job_item(self, key: str) -> Item:
        with _job_errors():
            found = self.worker.server.item(key)
        if found is None:
            raise JobError(f"{self.server.name} has no item {key}")
        return found

    def render(self, key: str) -> dict[str, Any]:
        worker = self.worker
        item = self._job_item(key)
        with _job_errors():
            worker.ctx.settings.action_days = worker.leaving_days()
            worker.ctx.settings.today = date.today()
            return self._render_plans(pipeline.plan_item(worker.ctx, item))

    def tmdb_preview(self, kind: Kind, tid: int, seasons: list[int]) -> dict[str, Any]:
        worker = self.worker
        with _job_errors():
            worker.ctx.settings.today = date.today()
            return self._render_plans(pipeline.plan_tmdb(worker.ctx, kind, tid, seasons))

    def why(self, key: str) -> dict[str, Any]:
        sources = self.worker.ctx.sources
        item = self._job_item(key)
        choices, marks = sources.choices, sources.marks
        sources.choices, sources.marks = why.ReadOnlyChoices(self.store), None
        try:
            with _job_errors():
                return why_json(why.report(self.worker.ctx, item, self.store))
        finally:
            sources.choices, sources.marks = choices, marks

    def change(self, key: str, action: Callable[[Worker], Outcome]) -> dict[str, str]:
        try:
            with _job_errors():
                outcome = action(self.worker)
        finally:
            self.images.drop(key)
        return {"outcome": outcome.value}

    def candidate_image(self, candidate: str) -> Image.Image:
        """Art from the Why list: TMDB, fanart.tv and Apple TV art come through the worker's own fetch."""
        if TMDB_PATH.match(candidate) or is_fanart(candidate) or apple.is_apple(candidate):
            return self.worker.ctx.sources.fetch(candidate).convert("RGB")
        return overrides.from_url(candidate)


def _art_source(req: Request, api: ItemsApi) -> Callable[[], Image.Image]:
    """How the job gets the image `art/set` was sent. Checked here, so a bad request fails before it is queued."""
    content_type = req.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if content_type.startswith("image/"):
        if not req.body:
            raise ApiError(400, "the body has no image")
        data = req.body
        return lambda: overrides.decode(data)
    body = req.json()
    url, candidate = body.get("url"), body.get("candidate")
    if isinstance(url, str) and url.strip():
        address = url.strip()
        if not address.startswith(("http://", "https://")):
            raise ApiError(400, "the address must start with http:// or https://")
        return lambda: overrides.from_url(address)
    if isinstance(candidate, str) and candidate:
        if not (TMDB_PATH.match(candidate) or candidate.startswith("https://")):
            raise ApiError(400, "the candidate must be a TMDB path or an https:// address from Why")
        return lambda: api.candidate_image(candidate)
    raise ApiError(400, 'send {"url"}, {"candidate"} or the image itself')


def _tmdb_request(req: Request) -> tuple[Kind, int, list[int]]:
    body = req.json()
    kind = body.get("kind")
    if kind not in ("movie", "tv"):
        raise ApiError(400, 'kind must be "movie" or "tv"')
    raw_id = body.get("id")
    tid = raw_id if isinstance(raw_id, int) else int(raw_id) if isinstance(raw_id, str) and raw_id.isdigit() else 0
    if isinstance(raw_id, bool) or tid <= 0:
        raise ApiError(400, "id must be a TMDB id, a whole number")
    seasons = body.get("seasons") or []
    if (
        not isinstance(seasons, list)
        or len(seasons) > MAX_SEASONS
        or not all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 1000 for n in seasons)
    ):
        raise ApiError(400, f"seasons must be a list of up to {MAX_SEASONS} season numbers")
    return ("movie" if kind == "movie" else "tv"), tid, sorted(set(seasons)) if kind == "tv" else []


def register(ui: "WebUi") -> None:  # noqa: C901
    """Add this module's routes to ui.router."""
    r = ui.router
    api = ItemsApi(ui)
    r.get("/api/libraries")(api.libraries)
    r.get("/api/items")(api.items)
    r.get("/api/search")(api.search)
    r.get("/api/items/{key}")(api.detail)
    r.get("/api/items/{key}/children")(api.children)
    r.get("/api/items/{key}/image")(api.image)
    r.get("/api/renders/{name}")(api.render_image)

    def titled(req: Request, *, required: bool = True) -> tuple[str, str]:
        key = _rating_key(req)
        with _server_errors():
            found = api.server.item(key)
        if found is None and required:
            raise ApiError(404, f"{api.server.name} has no item {key}")
        return key, _label(found, key)

    @r.post("/api/items/{key}/render")
    def render(req: Request) -> Response:
        key, name = titled(req)
        return ui.submit("render", f"Render {name}", lambda: api.render(key))

    @r.post("/api/items/{key}/why")
    def explain(req: Request) -> Response:
        key, name = titled(req)
        return ui.submit("why", f"Why {name}", lambda: api.why(key))

    @r.post("/api/items/{key}/art/next")
    def art_next(req: Request) -> Response:
        key, name = titled(req)
        return ui.submit("art-next", f"Next art for {name}", lambda: api.change(key, lambda w: w.next_art(key)))

    @r.post("/api/items/{key}/art/reset")
    def art_reset(req: Request) -> Response:
        key, name = titled(req)
        return ui.submit("art-reset", f"Automatic art for {name}", lambda: api.change(key, lambda w: w.reset_art(key)))

    @r.post("/api/items/{key}/art/set")
    def art_set(req: Request) -> Response:
        key, name = titled(req)
        load = _art_source(req, api)

        def run() -> dict[str, str]:
            with _job_errors():
                image = load()
            return api.change(key, lambda w: w.set_custom(key, image))

        return ui.submit("art-set", f"Custom art for {name}", run)

    @r.post("/api/items/{key}/forget")
    def forget(req: Request) -> Response:
        key, name = titled(req, required=False)

        def run(worker: Worker) -> Outcome:
            worker.store.forget(key)
            return worker.process(key)

        return ui.submit("forget", f"Forget {name}", lambda: api.change(key, run))

    @r.post("/api/items/{key}/process")
    def process(req: Request) -> Response:
        key, name = titled(req, required=False)
        return ui.submit("process", f"Process {name}", lambda: api.change(key, lambda w: w.process(key, force=True)))

    @r.post("/api/tools/tmdb-preview")
    def tmdb_preview(req: Request) -> Response:
        kind, tid, seasons = _tmdb_request(req)
        label = f"Preview TMDB {kind} {tid}" + (f" seasons {', '.join(map(str, seasons))}" if seasons else "")
        return ui.submit("tmdb-preview", label, lambda: api.tmdb_preview(kind, tid, seasons))
