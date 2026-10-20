"""What TMDB is missing for the library: logos, textless posters, and movies of collections the library has part of."""

import copy
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

from posteryard import http
from posteryard.artwork import MAX_CANDIDATES, TEXTLESS_RULE, UNCHECKED
from posteryard.config import Config
from posteryard.fanart import Fanart
from posteryard.server import Item, MediaServer, external_ids, tmdb_id
from posteryard.store import Store
from posteryard.tmdb import IMAGES, Images, Kind, Tmdb

log = logging.getLogger(__name__)
REPORT = "missing_report"
SITE = "https://www.themoviedb.org"
KINDS: dict[str, Kind] = {"movie": "movie", "show": "tv"}
NO_LOGO = "TMDB has no logo in {languages}"
NO_TEXTLESS = "TMDB has no textless poster"
ALL_TEXT = "Every textless poster on TMDB has text on it"
GIVE_UP = 25


def empty_report() -> dict[str, Any]:
    scan = {"state": "never", "checked": 0, "total": 0, "started": None, "finished": None, "error": None}
    return {"scan": scan, "languages": [], "logos": [], "textless": [], "collections": [], "unmatched": []}


class MissingScan:
    """Runs one scan at a time on its own thread. It only reads: the server, TMDB, fanart.tv and the cached choices."""

    def __init__(  # noqa: PLR0913
        self,
        cfg: Config,
        server: MediaServer,
        store: Store,
        *,
        tmdb: Tmdb | None = None,
        fanart: Fanart | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.cfg, self.server, self.store, self.today = cfg, server, store, today
        self.tmdb = tmdb or Tmdb(cfg.tmdb_api_key, cfg.logo_languages)
        self.fanart = fanart if fanart is not None else Fanart(cfg.fanart_api_key) if cfg.fanart_api_key else None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        try:
            self._report = json.loads(store.meta(REPORT)) if store.meta(REPORT) else empty_report()
        except ValueError:
            self._report = empty_report()
        if self._report["scan"]["state"] == "running":
            # A restart stopped it halfway.
            self._report["scan"].update(state="failed", error="Posteryard restarted during the check. Check again.")

    def report(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._report)

    def start(self) -> bool:
        with self._lock:
            if self._report["scan"]["state"] == "running":
                return False
            self._report["scan"] = {
                "state": "running", "checked": 0, "total": 0, "started": time.time(), "finished": None, "error": None,
            }  # fmt: skip
            self._thread = threading.Thread(target=self.run, daemon=True, name="missing")
        self._thread.start()
        return True

    def run(self) -> None:
        try:
            found = self._scan()
        except (http.RequestError, OSError, ValueError, LookupError) as exc:
            log.warning("missing art check failed", extra={"reason": str(exc)})
            with self._lock:
                self._report["scan"].update(state="failed", finished=time.time(), error=str(exc))
            self._save()
            return
        except Exception as exc:
            log.exception("missing art check failed")
            with self._lock:
                self._report["scan"].update(state="failed", finished=time.time(), error=f"{type(exc).__name__}: {exc}")
            self._save()
            return
        with self._lock:
            scan = {**self._report["scan"], "state": "done", "finished": time.time()}
            self._report = {**found, "scan": scan}
        self._save()
        log.info("missing art checked", extra={
            "logos": len(found["logos"]), "textless": len(found["textless"]),
            "collections": len(found["collections"]), "unmatched": len(found["unmatched"]),
        })  # fmt: skip

    def _save(self) -> None:
        self.store.set_meta(REPORT, json.dumps(self.report()))

    def _titles(self) -> Iterator[tuple[str, Item]]:
        for section in self.server.sections():
            kind = str(section.get("type"))
            if section.get("title") in self.cfg.libraries and kind in KINDS:
                for item in self.server.section_items(str(section["key"]), kind):
                    yield str(section.get("title")), item

    def _scan(self) -> dict[str, Any]:
        titles = sorted(self._titles(), key=lambda t: (str(t[1].get("title", "")).casefold(), t[1].get("year") or 0))
        with self._lock:
            self._report["scan"]["total"] = len(titles)
        languages = list(self.cfg.logo_languages)
        logos: list[dict[str, Any]] = []
        textless: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        owned: dict[int, dict[str, Any]] = {}
        collections: dict[int, list[int]] = {}
        failures = 0
        for library, item in titles:
            kind = KINDS[str(item.get("type"))]
            entry = {
                "key": str(item["ratingKey"]), "title": str(item.get("title", "")), "year": item.get("year"),
                "type": str(item.get("type")), "library": library, "tmdb_id": None, "reason": "", "tmdb_url": None,
            }  # fmt: skip
            try:
                tid = self._tmdb_id(item, kind)
                if tid is None:
                    unmatched.append({**entry, "reason": f"{self.server.name} has not matched it to TMDB"})
                    continue
                entry["tmdb_id"] = tid
                images = self.tmdb.images(kind, tid)
                if not self._has_logo(images, kind, tid):
                    reason = NO_LOGO.format(languages=" or ".join(code.upper() for code in languages))
                    logos.append({**entry, "reason": reason, "tmdb_url": f"{SITE}/{kind}/{tid}/images/logos"})
                if problem := self._textless_problem(images, kind, tid):
                    textless.append({**entry, "reason": problem, "tmdb_url": f"{SITE}/{kind}/{tid}/images/posters"})
                if kind == "movie":
                    owned[tid] = entry
                    ref = self.tmdb.details(kind, tid).get("belongs_to_collection")
                    if ref and ref.get("id"):
                        collections.setdefault(int(ref["id"]), []).append(tid)
                failures = 0
            except http.RequestError as exc:
                failures += 1
                log.debug("title not checked", extra={"title": entry["title"], "reason": str(exc)})
                if failures >= GIVE_UP:
                    raise
            finally:
                with self._lock:
                    self._report["scan"]["checked"] += 1
        return {
            "languages": languages, "logos": logos, "textless": textless, "unmatched": unmatched,
            "collections": self._collections(collections, owned),
        }  # fmt: skip

    def _tmdb_id(self, item: Item, kind: Kind) -> int | None:
        if (tid := tmdb_id(item)) is not None:
            return tid
        ids = external_ids(item)
        for source in ("imdb", "tvdb"):
            if source in ids and (found := self.tmdb.find(source, ids[source]).get(kind)) is not None:
                return found
        return None

    def _has_logo(self, images: Images, kind: Kind, tid: int) -> bool:
        languages = self.cfg.logo_languages
        if images.logos_in(languages):
            return True
        if self.fanart is None:
            return False
        tvdb = self.tmdb.tvdb_id(kind, tid) if kind == "tv" else None
        return bool(self.fanart.images(kind, tid, tvdb).images.logos_in(languages))

    def _textless_problem(self, images: Images, kind: Kind, tid: int) -> str | None:
        posters = images.textless_posters()
        if not posters:
            return NO_TEXTLESS
        # The service caches what it checked. Uncached posters are not read here: they count as fine.
        prefix = "" if self.cfg.text_check else UNCHECKED
        hit = self.store.get_choice(f"{prefix}textless{TEXTLESS_RULE}:{kind}:{tid}:posters")
        candidates = [r.path for r in posters[:MAX_CANDIDATES]]
        if hit is not None and hit.get("candidates") == candidates and not hit.get("path"):
            return ALL_TEXT
        return None

    def _collections(self, members: dict[int, list[int]], owned: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        today = self.today().isoformat()
        out: list[dict[str, Any]] = []
        for collection_id, tids in members.items():
            found = self.tmdb.collection(collection_id)
            if found is None:
                continue
            missing = []
            for part in found.get("parts") or []:
                if not part.get("id") or int(part["id"]) in owned:
                    continue
                released_on = str(part.get("release_date") or "")
                missing.append({
                    "tmdb_id": int(part["id"]), "title": str(part.get("title") or ""),
                    "year": int(released_on[:4]) if released_on[:4].isdigit() else None,
                    "release_date": released_on or None, "released": bool(released_on) and released_on <= today,
                    "poster_url": _poster(part.get("poster_path")), "tmdb_url": f"{SITE}/movie/{part['id']}",
                })  # fmt: skip
            if not missing:
                continue
            missing.sort(key=lambda m: (m["release_date"] or "9999", m["title"]))
            have = sorted(
                ({k: owned[t][k] for k in ("key", "tmdb_id", "title", "year")} for t in tids),
                key=lambda m: (m["year"] or 0, m["title"]),
            )
            out.append({
                "id": collection_id, "name": str(found.get("name") or ""),
                "poster_url": _poster(found.get("poster_path")), "tmdb_url": f"{SITE}/collection/{collection_id}",
                "owned": have, "missing": missing,
            })  # fmt: skip
        return sorted(out, key=lambda c: str(c["name"]).casefold())


def _poster(path: object) -> str | None:
    return f"{IMAGES}/w342{path}" if isinstance(path, str) and path.startswith("/") else None
