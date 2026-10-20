import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from posteryard import config, http
from posteryard.artwork import TEXTLESS_RULE, UNCHECKED
from posteryard.fanart import FanartImages
from posteryard.server import Item
from posteryard.store import Store
from posteryard.tmdb import Collection, CollectionPart, Details, ImageRef, Images, Kind
from posteryard.webui import api_missing
from posteryard.webui.missing import ALL_TEXT, NO_TEXTLESS, REPORT, MissingScan
from posteryard.webui.routing import Router
from tests.fakes import FakeServer, FakeTmdb
from tests.test_webui_core import answer, call, make_service


def _movie(key: str, title: str, year: int) -> Item:
    return Item(ratingKey=key, type="movie", title=title, year=year, Guid=[{"id": f"tmdb://{key}"}])


class Library(FakeServer):
    def __init__(self) -> None:
        self.titles: dict[str, list[Item]] = {
            "1": [
                _movie("104", "Dune", 2021), _movie("110", "John Wick: Chapter 4", 2023),
                _movie("200", "Arrival", 2016), _movie("300", "No Logo", 2020),
                Item(ratingKey="400", type="movie", title="By IMDb", Guid=[{"id": "imdb://tt400"}]),
                Item(ratingKey="500", type="movie", title="Unmatched"),
            ],
            "2": [Item(ratingKey="600", type="show", title="Severance", Guid=[{"id": "tvdb://371980"}])],
            "3": [_movie("700", "Elsewhere", 2001)],
        }  # fmt: skip

    def sections(self) -> list[Item]:
        return [
            Item(key="1", title="Movies", type="movie"),
            Item(key="2", title="TV Shows", type="show"),
            Item(key="3", title="Other", type="movie"),
        ]

    def section_items(self, section_key: str, kind: str, **filters: Any) -> Iterable[Item]:
        return self.titles.get(section_key, [])


class Catalogue(FakeTmdb):
    def __init__(self) -> None:
        super().__init__()
        self.fail: set[int] = set()
        self.calls: list[int] = []

    def details(self, kind: Kind, tmdb_id: int) -> Details:
        if tmdb_id in self.fail:
            raise http.RequestError("Timeout", f"https://api.themoviedb.org/3/{kind}/{tmdb_id}")
        collection = {104: {"id": 10, "name": "Dune"}, 110: {"id": 20, "name": "John Wick"}, 200: {"id": 99}}.get(
            tmdb_id
        )
        return {"title": str(tmdb_id), "belongs_to_collection": collection}  # type: ignore[typeddict-item]

    def images(self, kind: Kind, tmdb_id: int) -> Images:
        self.details(kind, tmdb_id)
        self.calls.append(tmdb_id)
        posters = [] if tmdb_id == 110 else [ImageRef(f"/{tmdb_id}-poster.jpg", None, 2000, 3000, 5, 1)]
        logos = [] if tmdb_id in (300, 371) else [ImageRef(f"/{tmdb_id}-logo.png", "en", 900, 300, 5, 1)]
        return Images(posters, [], logos)

    def find(self, source: str, external_id: str) -> dict[str, int]:
        return {"movie": 400} if external_id == "tt400" else {"tv": 371} if external_id == "371980" else {}

    def tvdb_id(self, kind: Kind, tmdb_id: int) -> int | None:
        return 371980

    def collection(self, collection_id: int) -> Collection | None:
        parts: dict[int, list[CollectionPart]] = {
            10: [{"id": 104, "title": "Dune", "release_date": "2021-09-15", "poster_path": "/dune.jpg"},
                 {"id": 105, "title": "Dune: Part Two", "release_date": "2024-02-27", "poster_path": "/two.jpg"},
                 {"id": 106, "title": "Dune: Part Three", "release_date": "2026-12-18"},
                 {"id": 107, "title": "Dune: Messiah"}],
            20: [{"id": 110, "title": "John Wick: Chapter 4", "release_date": "2023-03-22"}],
        }  # fmt: skip
        if collection_id not in parts:
            return None
        return {"id": collection_id, "name": f"Collection {collection_id}", "parts": parts[collection_id]}


class Logos:
    def __init__(self, has: bool) -> None:
        self.has = has

    def images(self, kind: Kind, tmdb_id: int, tvdb_id: int | None) -> FanartImages:
        logos = [ImageRef("https://assets.fanart.tv/logo.png", "en", 800, 300, 1, 1)] if self.has else []
        return FanartImages(Images([], [], logos))


def make(tmp_path: Path, **env: str) -> tuple[MissingScan, Catalogue, Store]:
    cfg = config.load({"TMDB_API_KEY": "example", "DATA_DIR": str(tmp_path), **env})
    store, tmdb = Store(cfg.state_path), Catalogue()
    scan = MissingScan(cfg, Library(), store, tmdb=tmdb, today=lambda: date(2026, 10, 19))
    return scan, tmdb, store


def scanned(scan: MissingScan) -> dict[str, Any]:
    assert scan.start()
    assert scan._thread is not None
    scan._thread.join(5)
    return scan.report()


def test_titles_without_a_logo_or_a_textless_poster(tmp_path: Path) -> None:
    report = scanned(make(tmp_path)[0])
    assert report["scan"] | {"started": 0, "finished": 0} == {
        "state": "done", "checked": 7, "total": 7, "started": 0, "finished": 0, "error": None,
    }  # fmt: skip
    assert report["languages"] == ["en"]
    assert [(t["title"], t["library"]) for t in report["logos"]] == [("No Logo", "Movies"), ("Severance", "TV Shows")]
    assert report["logos"][0]["tmdb_url"] == "https://www.themoviedb.org/movie/300/images/logos"
    assert report["logos"][1]["tmdb_url"] == "https://www.themoviedb.org/tv/371/images/logos"
    assert [(t["title"], t["reason"]) for t in report["textless"]] == [("John Wick: Chapter 4", NO_TEXTLESS)]
    assert [t["title"] for t in report["unmatched"]] == ["Unmatched"]


def test_fanart_logos_count(tmp_path: Path) -> None:
    scan, _, _ = make(tmp_path)
    scan.fanart = Logos(has=True)  # type: ignore[assignment]
    assert scanned(scan)["logos"] == []
    scan.fanart = Logos(has=False)  # type: ignore[assignment]
    assert len(scanned(scan)["logos"]) == 2


@pytest.mark.parametrize(("env", "prefix"), [({}, ""), ({"TEXT_CHECK": "false"}, UNCHECKED)])
def test_posters_rejected_for_text_come_from_the_cached_choice(
    tmp_path: Path, env: dict[str, str], prefix: str
) -> None:
    scan, _, store = make(tmp_path, **env)
    store.put_choice(f"{prefix}textless{TEXTLESS_RULE}:movie:200:posters", {"candidates": ["/200-poster.jpg"]})
    store.put_choice(f"{prefix}textless{TEXTLESS_RULE}:movie:104:posters", {"candidates": ["/old.jpg"]})
    store.put_choice(
        f"{prefix}textless{TEXTLESS_RULE}:movie:300:posters", {"candidates": ["/300-poster.jpg"], "path": "/a.jpg"}
    )
    found = {t["title"]: t["reason"] for t in scanned(scan)["textless"]}
    assert found == {"Arrival": ALL_TEXT, "John Wick: Chapter 4": NO_TEXTLESS}


def test_collections_list_the_movies_the_library_lacks(tmp_path: Path) -> None:
    collections = scanned(make(tmp_path)[0])["collections"]
    assert [c["name"] for c in collections] == ["Collection 10"]
    dune = collections[0]
    assert dune["tmdb_url"] == "https://www.themoviedb.org/collection/10"
    assert dune["owned"] == [{"key": "104", "tmdb_id": 104, "title": "Dune", "year": 2021}]
    assert [(m["title"], m["year"], m["released"]) for m in dune["missing"]] == [
        ("Dune: Part Two", 2024, True), ("Dune: Part Three", 2026, False), ("Dune: Messiah", None, False),
    ]  # fmt: skip
    assert dune["missing"][0]["poster_url"] == "https://image.tmdb.org/t/p/w342/two.jpg"
    assert dune["missing"][1]["poster_url"] is None


def test_a_title_tmdb_cannot_read_is_skipped(tmp_path: Path) -> None:
    scan, tmdb, _ = make(tmp_path)
    tmdb.fail = {300}
    report = scanned(scan)
    assert report["scan"]["state"] == "done"
    assert "No Logo" not in [t["title"] for t in report["logos"]]


def test_a_tmdb_outage_fails_the_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scan, tmdb, store = make(tmp_path)
    monkeypatch.setattr("posteryard.webui.missing.GIVE_UP", 2)
    tmdb.fail = {104, 110, 200, 300, 400, 371}
    report = scanned(scan)
    assert report["scan"]["state"] == "failed"
    assert report["scan"]["error"].startswith("Timeout")
    assert json.loads(store.meta(REPORT))["scan"]["state"] == "failed"


def test_an_unexpected_error_fails_the_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scan, tmdb, _ = make(tmp_path)

    def broken(collection_id: int) -> Collection | None:
        raise TypeError("parts")

    monkeypatch.setattr(tmdb, "collection", broken)
    assert scanned(scan)["scan"]["error"] == "TypeError: parts"


def test_one_scan_at_a_time_and_the_report_survives_a_restart(tmp_path: Path) -> None:
    scan, _, store = make(tmp_path)
    assert scan.report()["scan"]["state"] == "never"
    scan._report["scan"]["state"] = "running"
    assert not scan.start()
    scan._report["scan"]["state"] = "done"
    done = scanned(scan)
    again = MissingScan(scan.cfg, scan.server, store, tmdb=Catalogue())
    assert again.report() == done
    store.set_meta(REPORT, json.dumps({**done, "scan": {**done["scan"], "state": "running"}}))
    stopped = MissingScan(scan.cfg, scan.server, store, tmdb=Catalogue()).report()["scan"]
    assert (stopped["state"], stopped["error"]) == ("failed", "Posteryard restarted during the check. Check again.")
    store.set_meta(REPORT, "{broken")
    assert MissingScan(scan.cfg, scan.server, store, tmdb=Catalogue()).report()["scan"]["state"] == "never"


def test_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    ui = service.ui
    assert ui is not None
    scan = MissingScan(service.cfg, Library(), service.store, tmdb=Catalogue())
    ui.router = Router()
    api_missing.register(ui, scan)
    assert answer(call(ui, "GET", "/api/missing"))["scan"]["state"] == "never"
    monkeypatch.setattr(scan, "start", lambda: True)
    assert answer(call(ui, "POST", "/api/missing/scan")) == {"ok": True}
    monkeypatch.setattr(scan, "start", lambda: False)
    response = call(ui, "POST", "/api/missing/scan")
    assert (response.status, answer(response)) == (409, {"error": "A check is already running"})
    assert call(ui, "GET", "/api/missing", signed_in=False).status == 401
