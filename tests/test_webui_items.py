import io
import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from posteryard import config, http, overrides
from posteryard.jellyfin import Emby, Jellyfin
from posteryard.notify import Notifier
from posteryard.plex import Plex
from posteryard.server import Item
from posteryard.service import Service
from posteryard.store import Store
from posteryard.webui import api_items
from posteryard.webui.api_items import BlobCache, TtlCache
from posteryard.webui.app import WebUi, _Headers
from posteryard.webui.auth import COOKIE
from posteryard.webui.routing import Request, Response, parse_query
from posteryard.worker import Worker
from tests.fakes import FakeServer, FakeTmdb, fetch, read, ref


def _item(key: str, kind: str, title: str, section: str, library: str, **extra: Any) -> Item:
    fields = {
        "ratingKey": key, "type": kind, "title": title, "librarySectionID": section, "librarySectionTitle": library,
        "Media": [], "thumb": f"/library/metadata/{key}/thumb", **extra,
    }  # fmt: skip
    return cast(Item, fields)


def jpeg(colour: tuple[int, int, int] = (10, 120, 200), size: tuple[int, int] = (600, 900)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


class Library(FakeServer):
    """Two managed libraries, one that is not, a collection, a show with seasons and episodes."""

    def __init__(self) -> None:
        tmdb = [{"id": "tmdb://603"}]
        self.items: dict[str, Item] = {
            "10": _item("10", "movie", "The Matrix", "1", "Movies", year=1999, addedAt=100, updatedAt=500, Guid=tmdb,
                        Label=[{"tag": "posteryard-custom"}, {"tag": "Favourite"}]),
            "11": _item("11", "movie", "Arrival", "1", "Movies", year=2016, addedAt=300, updatedAt=300, Guid=tmdb),
            "12": _item("12", "movie", "the Abyss", "1", "Movies", year=1989, addedAt=200, updatedAt=900, Guid=tmdb),
            "13": _item("13", "movie", "Dune", "1", "Movies", year=2021, addedAt=400, Guid=tmdb),
            "14": _item("14", "movie", "Dune", "1", "Movies", year=1984, addedAt=50, Guid=tmdb),
            "15": _item("15", "movie", "No Match", "1", "Movies", addedAt=10),
            "20": _item("20", "show", "Severance", "2", "TV Shows", year=2022, addedAt=250, Guid=tmdb, childCount=2),
            "21": _item("21", "season", "Season 1", "2", "TV Shows", index=1, parentRatingKey="20",
                        parentTitle="Severance"),
            "22": _item("22", "season", "Season 2", "2", "TV Shows", index=2, parentRatingKey="20"),
            "23": _item("23", "episode", "Good News About Hell", "2", "TV Shows", index=1, parentIndex=1,
                        parentRatingKey="21", grandparentRatingKey="20", parentTitle="Season 1",
                        grandparentTitle="Severance"),
            "24": _item("24", "episode", "Half Loop", "2", "TV Shows", index=2, parentIndex=1, parentRatingKey="21",
                        grandparentRatingKey="20"),
            "30": _item("30", "collection", "Sci-Fi", "1", "Movies", addedAt=20),
            "40": _item("40", "movie", "Birthday", "3", "Home Videos", addedAt=30),
        }  # fmt: skip
        self.kids = {"20": ["21", "22"], "21": ["23", "24"], "22": [], "30": ["10", "11"]}
        self.listed: list[tuple[str, str]] = []
        self.poster: bytes = jpeg()
        self.fail: Exception | None = None

    def sections(self) -> list[Item]:
        self._check()
        return [
            Item(key="1", title="Movies", type="movie"),
            Item(key="2", title="TV Shows", type="show"),
            Item(key="3", title="Home Videos", type="movie"),
            Item(key="4", title="Music", type="artist"),
        ]

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def item(self, rating_key: str) -> Item | None:
        self._check()
        found = self.items.get(rating_key)
        return Item(**found) if found else None

    def children(self, rating_key: str) -> list[Item]:
        self._check()
        return [Item(**self.items[k]) for k in self.kids.get(rating_key, [])]

    def collection_children(self, rating_key: str) -> list[Item]:
        return self.children(rating_key)

    def section_items(self, section_key: str, kind: str, **filters: Any) -> Iterable[Item]:
        self._check()
        self.listed.append((section_key, kind))
        return [Item(**i) for i in self.items.values() if i["librarySectionID"] == section_key and i["type"] == kind]

    def poster_bytes(self, item: Item) -> bytes:
        self._check()
        return self.poster


@pytest.fixture
def library() -> Library:
    return Library()


def make_ui(tmp_path: Path, server: Library, **env: str) -> WebUi:
    cfg = config.load({
        "TMDB_API_KEY": "example", "DATA_DIR": str(tmp_path), "WEBHOOK_SECRET": "example-secret",
        "UI_PASSWORD": "pw", "EPISODE_THUMBNAILS": "titled", **env,
    })  # fmt: skip
    store = Store(cfg.state_path)
    worker = Worker(cfg, server, store, Notifier(), FakeTmdb([ref("/clean.jpg", None), ref("/second.jpg", None)]))
    worker.ctx.sources.read, worker.ctx.sources.fetch = read, fetch
    service = Service(cfg, server, store, worker)
    ui = WebUi(tmp_path, "pw", cfg=cfg, service=service)
    service.ui = ui
    ui.jobs.dispatch = ui.jobs.execute
    return ui


@pytest.fixture
def ui(tmp_path: Path, library: Library) -> WebUi:
    return make_ui(tmp_path, library)


def call(ui: WebUi, method: str, path: str, body: object = None, headers: dict[str, str] | None = None) -> Response:
    sent = {"Cookie": f"{COOKIE}={ui.auth.issue()}", "X-Requested-With": "posteryard", **(headers or {})}
    raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
    path, _, query = path.partition("?")
    return ui.dispatch(Request(method, path, parse_query(query), _Headers(sent), raw))


def get(ui: WebUi, path: str, status: int = 200) -> Any:
    response = call(ui, "GET", path)
    assert response.status == status, response.body
    return json.loads(response.body)


def job(ui: WebUi, path: str, body: object = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = call(ui, "POST", path, body, headers)
    assert response.status == 202, response.body
    found: dict[str, Any] = get(ui, f"/api/jobs/{json.loads(response.body)['job']}")
    return found


def done(ui: WebUi, path: str, body: object = None, headers: dict[str, str] | None = None) -> Any:
    found = job(ui, path, body, headers)
    assert found["state"] == "done", found["error"]
    return found["result"]


def store_of(ui: WebUi) -> Store:
    assert ui.service is not None
    return ui.service.store


def titles(answer: dict[str, Any]) -> list[str]:
    return [f"{i['title']} {i['year']}" if i["year"] else i["title"] for i in answer["items"]]


# Libraries and listing


def test_libraries_list_movie_and_tv_libraries_with_counts(ui: WebUi) -> None:
    assert get(ui, "/api/libraries")["libraries"] == [
        {"key": "1", "title": "Movies", "type": "movie", "managed": True, "count": 6},
        {"key": "2", "title": "TV Shows", "type": "show", "managed": True, "count": 1},
        {"key": "3", "title": "Home Videos", "type": "movie", "managed": False, "count": 1},
    ]


def test_items_list_every_managed_title_by_name(ui: WebUi) -> None:
    found = get(ui, "/api/items")
    assert found["total"] == 7
    assert titles(found) == [
        "Arrival 2016", "Dune 1984", "Dune 2021", "No Match", "Severance 2022", "the Abyss 1989", "The Matrix 1999"
    ]  # fmt: skip
    matrix = found["items"][-1]
    assert matrix == {
        "key": "10", "title": "The Matrix", "year": 1999, "type": "movie", "library": "Movies", "library_key": "1",
        "added_at": 100, "updated_at": 500, "labels": ["posteryard-custom"], "parent_key": None, "index": None,
        "status": None, "override": None,
    }  # fmt: skip


def test_items_sort_by_added_and_updated(ui: WebUi) -> None:
    assert titles(get(ui, "/api/items?sort=added&limit=3")) == ["Dune 2021", "Arrival 2016", "Severance 2022"]
    # Without an updated time, the added time counts.
    assert titles(get(ui, "/api/items?sort=updated&limit=3")) == ["the Abyss 1989", "The Matrix 1999", "Dune 2021"]


def test_items_filter_by_name_kind_library_and_page(ui: WebUi) -> None:
    assert titles(get(ui, "/api/items?q=DUNE")) == ["Dune 1984", "Dune 2021"]
    assert titles(get(ui, "/api/items?kind=show")) == ["Severance 2022"]
    assert titles(get(ui, "/api/items?kind=collection")) == ["Sci-Fi"]
    assert titles(get(ui, "/api/items?library=3")) == ["Birthday"]
    assert titles(get(ui, "/api/items?library=Home Videos")) == ["Birthday"]
    assert get(ui, "/api/items?library=2&kind=movie") == {"total": 0, "items": []}
    page = get(ui, "/api/items?offset=2&limit=2")
    assert page["total"] == 7
    assert titles(page) == ["Dune 2021", "No Match"]


def test_items_filter_by_status_and_show_overrides(ui: WebUi) -> None:
    store = store_of(ui)
    store.previewed("11", "poster", "Arrival", "abc")
    store.failed("12", "poster", "the Abyss", "boom")
    store.manual("13", "poster", "Dune")
    store.set_custom("11", "/data/custom/11.jpg", "command")
    store.add_skip("12", "/old.jpg")
    assert titles(get(ui, "/api/items?status=preview")) == ["Arrival 2016"]
    assert titles(get(ui, "/api/items?status=failed")) == ["the Abyss 1989"]
    assert len(get(ui, "/api/items?status=none")["items"]) == 4
    found = {i["key"]: i for i in get(ui, "/api/items")["items"]}
    assert (found["11"]["status"], found["11"]["override"]) == ("preview", "custom")
    assert (found["12"]["status"], found["12"]["override"]) == ("failed", "skip")
    assert found["13"]["override"] is None
    assert titles(get(ui, "/api/items?override=custom")) == ["Arrival 2016"]
    assert titles(get(ui, "/api/items?override=skip")) == ["the Abyss 1989"]


@pytest.mark.parametrize(
    "query", ["sort=year", "status=done", "override=other", "kind=season", "limit=x", "offset=-"]
)  # fmt: skip
def test_items_refuse_unknown_filters(ui: WebUi, query: str) -> None:
    assert "error" in get(ui, f"/api/items?{query}", 400)


def test_an_unknown_library_is_not_found(ui: WebUi) -> None:
    assert get(ui, "/api/items?library=Music", 404) == {"error": "Plex has no movie or TV library Music"}


def test_listings_are_cached_until_refresh(ui: WebUi, library: Library) -> None:
    get(ui, "/api/items")
    get(ui, "/api/items?sort=added")
    get(ui, "/api/search?q=dune")
    assert library.listed == [("1", "movie"), ("2", "show")]
    get(ui, "/api/items?refresh=1")
    assert library.listed[2:] == [("1", "movie"), ("2", "show")]


def test_a_server_error_answers_502(ui: WebUi, library: Library) -> None:
    library.fail = http.RequestError("ConnectionError", "http://plex.example:32400/library/sections")
    for path in ("/api/items", "/api/libraries", "/api/items/10", "/api/items/10/children"):
        assert get(ui, path, 502)["error"].startswith("ConnectionError")
    assert call(ui, "POST", "/api/items/10/render").status == 502


def test_ttl_cache_expires_and_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    cache = TtlCache(60, 2)
    loads: list[str] = []

    def load(name: str) -> Callable[[], str]:
        def run() -> str:
            loads.append(name)
            return name

        return run

    assert cache.get(("a",), load("a")) == "a"
    cache.get(("a",), load("a"))
    now[0] += 61
    cache.get(("a",), load("a"))
    cache.get(("b",), load("b"))
    cache.get(("c",), load("c"))
    cache.get(("a",), load("a"))
    assert loads == ["a", "a", "b", "c", "a"]


# Search


def test_search_finds_titles_closest_first(ui: WebUi) -> None:
    assert titles(get(ui, "/api/search?q=dune")) == ["Dune 1984", "Dune 2021"]
    assert titles(get(ui, "/api/search?q=severence")) == ["Severance 2022"]
    assert get(ui, "/api/search?q=") == {"items": []}


def test_search_stops_at_twelve(tmp_path: Path, library: Library) -> None:
    for number in range(20):
        key = str(100 + number)
        library.items[key] = _item(key, "movie", f"Saw {number}", "1", "Movies")
    assert len(get(make_ui(tmp_path, library), "/api/search?q=saw")["items"]) == 12


# One title


def test_movie_detail(ui: WebUi, tmp_path: Path) -> None:
    store = store_of(ui)
    store.previewed("10", "poster", "The Matrix", "fp1")
    store.failed("10", "item", "The Matrix", "TMDB is down")
    store.set_custom("10", "/data/custom/10.jpg", "plex")
    (tmp_path / "previews").mkdir()
    (tmp_path / "previews" / "10-poster.jpg").write_bytes(jpeg())
    found = get(ui, "/api/items/10")
    assert found["title"] == "The Matrix"
    assert found["status"] == "preview"
    assert found["tmdb_id"] == 603
    assert found["server_url"] is None
    assert (found["parent"], found["grandparent"]) == (None, None)
    assert [(r["target"], r["status"], r["fingerprint"]) for r in found["records"]] == [("poster", "preview", "fp1")]
    assert found["item_error"]["last_error"] == "TMDB is down"
    assert found["item_error"]["failures"] == 1
    assert found["override"] == {"custom": True, "source": "plex", "skip": []}
    assert found["ignored"] is False
    assert found["all_labels"] == ["posteryard-custom", "Favourite"]
    assert found["targets"] == ["poster", "art"]
    assert found["has_preview"] == {"poster": True, "art": False}
    assert found["children_count"] == 0


def test_show_season_and_episode_detail(ui: WebUi, library: Library) -> None:
    library.items["20"]["Label"] = [{"tag": "Posteryard-Ignore"}]
    show = get(ui, "/api/items/20")
    assert (show["children_count"], show["ignored"], show["labels"]) == (2, True, ["posteryard-ignore"])
    season = get(ui, "/api/items/22")
    assert season["parent"] == {"key": "20", "title": "Severance"}
    assert season["targets"] == ["poster"]
    assert season["children_count"] == 0
    episode = get(ui, "/api/items/24")
    assert episode["parent"] == {"key": "21", "title": "Season 1"}
    assert episode["grandparent"] == {"key": "20", "title": "Severance"}
    assert episode["targets"] == ["thumb"]
    collection = get(ui, "/api/items/30")
    assert (collection["type"], collection["children_count"]) == ("collection", 2)


def test_detail_of_a_missing_or_malformed_key(ui: WebUi) -> None:
    assert get(ui, "/api/items/999", 404) == {"error": "Plex has no item 999"}
    assert get(ui, "/api/items/nope", 400) == {"error": "not a valid item key"}
    assert get(ui, "/api/items/999/children", 404)["error"] == "Plex has no item 999"


def test_children_of_a_show_and_of_a_season(ui: WebUi) -> None:
    store_of(ui).previewed("23", "thumb", "Good News About Hell", "x")
    seasons = get(ui, "/api/items/20/children")["items"]
    assert [(s["key"], s["index"], s["parent_key"], s["library"]) for s in seasons] == [
        ("21", 1, "20", "TV Shows"), ("22", 2, "20", "TV Shows")
    ]  # fmt: skip
    episodes = get(ui, "/api/items/21/children")["items"]
    assert [(e["title"], e["status"]) for e in episodes] == [("Good News About Hell", "preview"), ("Half Loop", None)]


def test_server_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    plex = Plex("http://plex:32400", "token")
    monkeypatch.setattr(plex, "machine_id", lambda: "abc123")
    assert api_items.server_url(plex, "42") == (
        "https://app.plex.tv/desktop/#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F42"
    )

    def down() -> str:
        raise http.RequestError("ConnectionError", "http://plex:32400/identity")

    monkeypatch.setattr(plex, "machine_id", down)
    assert api_items.server_url(plex, "42") is None
    key = "a" * 32
    assert api_items.server_url(Jellyfin("http://jf:8096/", "k"), key) == (
        f"http://jf:8096/web/index.html#!/details?id={key}"
    )
    assert api_items.server_url(Emby("http://emby:8096", "k"), "42") == "http://emby:8096/web/index.html#!/item?id=42"
    assert api_items.server_url(FakeServer(), "42") is None


# Images


def image_of(response: Response) -> Image.Image:
    assert response.status == 200, response.body
    assert response.content_type == "image/jpeg"
    assert response.headers["Cache-Control"] == "private, max-age=60"
    return Image.open(io.BytesIO(response.body))


def test_image_from_the_preview_resized_and_cached(ui: WebUi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "previews").mkdir()
    preview = tmp_path / "previews" / "10-poster.jpg"
    preview.write_bytes(jpeg((200, 10, 10), (1000, 1500)))
    assert image_of(call(ui, "GET", "/api/items/10/image")).size == (1000, 1500)
    assert image_of(call(ui, "GET", "/api/items/10/image?source=preview&w=200")).size == (200, 300)
    resized: list[int] = []
    original = api_items.resize_jpeg

    def resize(data: bytes, width: int, *, trusted: bool) -> bytes:
        resized.append(width)
        return original(data, width, trusted=trusted)

    monkeypatch.setattr(api_items, "resize_jpeg", resize)
    call(ui, "GET", "/api/items/10/image?w=200")
    assert resized == []
    preview.write_bytes(jpeg((10, 200, 10), (1000, 1500)))
    os.utime(preview, ns=(1, 1))
    picture = image_of(call(ui, "GET", "/api/items/10/image?w=5"))
    assert resized == [16]
    assert picture.size == (16, 24)
    green = picture.convert("RGB").getpixel((8, 12))
    assert isinstance(green, tuple)
    assert green[1] > 150


def test_image_from_the_server(ui: WebUi, library: Library) -> None:
    assert image_of(call(ui, "GET", "/api/items/10/image")).size == (600, 900)
    assert image_of(call(ui, "GET", "/api/items/11/image?source=server&w=100")).size == (100, 150)
    buffer = io.BytesIO()
    Image.new("RGBA", (300, 450)).save(buffer, "PNG")
    library.poster = buffer.getvalue()
    assert image_of(call(ui, "GET", "/api/items/12/image?source=server")).size == (300, 450)


def test_background_from_the_server(ui: WebUi, library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    def image_bytes(item: Item, target: str) -> bytes:
        return jpeg(size=(1920, 1080)) if target == "art" else b""

    monkeypatch.setattr(library, "image_bytes", image_bytes)
    assert image_of(call(ui, "GET", "/api/items/10/image?target=art&w=480")).size == (480, 270)
    assert get(ui, "/api/items/10/image?target=thumb", 404)["error"] == "Plex has no thumbnail for this item"


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/items/10/image?source=preview", 404),
        ("/api/items/10/image?target=art", 404),
        ("/api/items/999/image", 404),
        ("/api/items/x/image", 400),
        ("/api/items/10/image?target=banner", 400),
        ("/api/items/10/image?source=web", 400),
        ("/api/items/10/image?w=wide", 400),
    ],
)
def test_image_errors(ui: WebUi, path: str, status: int) -> None:
    assert "error" in get(ui, path, status)


def test_image_errors_from_the_server(ui: WebUi, library: Library) -> None:
    library.poster = b""
    assert get(ui, "/api/items/10/image", 404)["error"] == "Plex has no poster for this item"
    library.poster = b"not an image"
    assert "could not be read" in get(ui, "/api/items/11/image", 502)["error"]
    library.fail = http.HttpError(404, "http://plex.example:32400/library/metadata/12/thumb")
    assert get(ui, "/api/items/12/image", 502)["error"].startswith("HTTP 404")


def test_poster_bytes_failures(tmp_path: Path, library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    ui = make_ui(tmp_path, library)
    errors: list[Exception] = [
        http.HttpError(404, "http://plex.example:32400/x"),
        http.HttpError(500, "http://plex.example:32400/x"),
        http.RequestError("Timeout", "http://plex.example:32400/x"),
    ]

    def poster_bytes(item: Item) -> bytes:
        raise errors.pop(0)

    monkeypatch.setattr(library, "poster_bytes", poster_bytes)
    assert get(ui, "/api/items/10/image", 404)["error"] == "Plex has no poster for this item"
    assert get(ui, "/api/items/10/image", 502)["error"].startswith("HTTP 500")
    assert get(ui, "/api/items/10/image", 502)["error"].startswith("Timeout")


def test_a_plex_item_without_a_poster(tmp_path: Path, library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    ui = make_ui(tmp_path, library)
    plex = Plex("http://plex:32400", "token")
    monkeypatch.setattr(plex, "item", lambda key: {"ratingKey": key, "type": "movie", "title": "Bare"})
    assert ui.service is not None
    ui.service.server = plex
    assert get(ui, "/api/items/10/image", 404)["error"] == "Plex has no poster for this item"


def test_blob_cache_drops_the_least_used_and_by_key() -> None:
    cache = BlobCache(10, 3)
    cache.put(("a", "1"), b"1234")
    cache.put(("b", "1"), b"1234")
    cache.put(("a", "2"), b"1234")
    assert cache.get(("a", "1")) is None
    assert cache.get(("b", "1")) == b"1234"
    cache.put(("c", "1"), b"1")
    cache.put(("c", "1"), b"12")
    cache.put(("d", "1"), b"1")
    assert cache.get(("a", "2")) is None
    cache.drop("b")
    assert (cache.get(("b", "1")), cache.get(("c", "1"))) == (None, b"12")


def test_image_url_of_each_kind_of_art() -> None:
    assert api_items.image_url("/abc.jpg", "w342") == "https://image.tmdb.org/t/p/w342/abc.jpg"
    assert api_items.image_url("https://assets.fanart.tv/x.jpg", "w342") == "https://assets.fanart.tv/x.jpg"
    assert api_items.image_url("file:/data/custom/1.jpg", "w342") is None
    assert api_items.image_url("//evil.example/x.jpg", "w342") is None
    assert api_items.image_url(None, "w342") is None


# Jobs


def test_render_shows_every_image_without_saving(ui: WebUi, tmp_path: Path) -> None:
    result = done(ui, "/api/items/10/render")
    renders = result["renders"]
    assert [(r["target"], r["name"], r["changed"]) for r in renders] == [
        ("poster", "The Matrix", True), ("art", "The Matrix", True)
    ]  # fmt: skip
    assert renders[0]["notes"] == ["art from TMDB /clean.jpg"]
    assert not (tmp_path / "previews").exists()
    assert store_of(ui).get("10", "poster") is None
    picture = call(ui, "GET", renders[0]["url"])
    assert (picture.status, picture.content_type) == (200, "image/jpeg")
    assert Image.open(io.BytesIO(picture.body)).format == "JPEG"
    assert get(ui, "/api/renders/0123456789abcdef.jpg", 404)["error"] == "that render is gone. Render again."
    assert get(ui, "/api/renders/nope", 404)
    done(ui, "/api/items/10/process")
    assert [r["changed"] for r in done(ui, "/api/items/10/render")["renders"]] == [False, False]


def test_render_a_season_and_an_episode(ui: WebUi) -> None:
    season = job(ui, "/api/items/21/render")
    assert season["label"] == "Render Severance · Season 1"
    assert [r["name"] for r in season["result"]["renders"]] == ["Severance · Season 1"]
    episode = job(ui, "/api/items/23/render")
    assert episode["label"] == "Render Severance · Good News About Hell"
    assert [r["target"] for r in episode["result"]["renders"]] == ["thumb"]


def test_render_failures(ui: WebUi) -> None:
    failed = job(ui, "/api/items/15/render")
    assert failed["state"] == "failed"
    assert "has no TMDB id" in failed["error"]
    assert get(ui, "/api/items/999", 404)
    assert call(ui, "POST", "/api/items/999/render").status == 404
    assert call(ui, "POST", "/api/items/bad/render").status == 400


def test_a_job_whose_item_is_gone_by_then(tmp_path: Path, library: Library) -> None:
    ui = make_ui(tmp_path, library)
    queued: list[Any] = []
    ui.jobs.dispatch = queued.append
    call(ui, "POST", "/api/items/10/render")
    del library.items["10"]
    ui.jobs.execute(queued[0])
    assert (queued[0].state, queued[0].error) == ("failed", "Plex has no item 10")


def test_why_lists_candidates_and_restores_the_context(ui: WebUi) -> None:
    assert ui.service is not None
    sources = ui.service.worker.ctx.sources
    choices, marks = sources.choices, sources.marks
    found = job(ui, "/api/items/10/why")
    assert found["label"] == "Why The Matrix (1999)"
    result = found["result"]
    assert (result["art"], result["note"]) == ("/clean.jpg", "art from TMDB /clean.jpg")
    assert result["art_url"] == "https://image.tmdb.org/t/p/w500/clean.jpg"
    assert result["logo_url"] == "https://image.tmdb.org/t/p/w500/logo.png"
    first = result["groups"][0]
    assert first["name"] == "TMDB posters"
    assert first["candidates"][0] == {
        "path": "/clean.jpg", "thumb_url": "https://image.tmdb.org/t/p/w342/clean.jpg",
        "full_url": "https://image.tmdb.org/t/p/original/clean.jpg", "width": 2000, "height": 3000,
        "votes": "rated 5.0 (10 votes)", "verdict": "used", "used": True, "skipped": False, "clean": True,
    }  # fmt: skip
    assert (sources.choices, sources.marks) == (choices, marks)
    failed = job(ui, "/api/items/15/why")
    assert failed["state"] == "failed"
    assert (sources.choices, sources.marks) == (choices, marks)


def test_art_next_then_reset(ui: WebUi) -> None:
    found = job(ui, "/api/items/10/art/next")
    assert (found["label"], found["result"]) == ("Next art for The Matrix (1999)", {"outcome": "preview"})
    assert get(ui, "/api/items/10")["override"] == {"custom": False, "source": "", "skip": ["/clean.jpg"]}
    why = done(ui, "/api/items/10/why")
    verdicts = {c["path"]: c["verdict"] for c in why["groups"][0]["candidates"]}
    assert verdicts == {"/clean.jpg": "skipped by art next", "/second.jpg": "used"}
    assert done(ui, "/api/items/10/art/reset") == {"outcome": "preview"}
    assert get(ui, "/api/items/10")["override"] is None
    assert job(ui, "/api/items/15/art/next")["error"].endswith(
        "has no TMDB id, and TMDB knows no IMDb or TVDB id of it"
    )


def test_art_set_from_a_candidate_url_or_upload(ui: WebUi, monkeypatch: pytest.MonkeyPatch) -> None:
    assert done(ui, "/api/items/10/art/set", {"candidate": "/second.jpg"}) == {"outcome": "preview"}
    override = get(ui, "/api/items/10")["override"]
    assert (override["custom"], override["source"]) == (True, "command")
    why = done(ui, "/api/items/10/why")
    assert why["art"].startswith("file:")
    assert why["art_url"] is None
    asked: list[str] = []

    def from_url(url: str) -> Image.Image:
        asked.append(url)
        return Image.new("RGB", (600, 900), (1, 2, 3))

    monkeypatch.setattr(overrides, "from_url", from_url)
    assert done(ui, "/api/items/11/art/set", {"url": " https://example.com/a.jpg "}) == {"outcome": "preview"}
    assert done(ui, "/api/items/12/art/set", {"candidate": "https://example.com/b.jpg"}) == {"outcome": "preview"}
    assert asked == ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    upload = done(ui, "/api/items/13/art/set", jpeg(), {"Content-Type": "image/jpeg"})
    assert upload == {"outcome": "preview"}
    assert get(ui, "/api/items/13")["override"]["custom"] is True


def test_art_set_refuses_bad_requests(ui: WebUi) -> None:
    for body, message in (
        ({}, 'send {"url"}, {"candidate"} or the image itself'),
        ({"url": "file:///etc/passwd"}, "the address must start with http:// or https://"),
        ({"candidate": "../x"}, "the candidate must be a TMDB path or an https:// address from Why"),
    ):
        response = call(ui, "POST", "/api/items/10/art/set", body)
        assert (response.status, json.loads(response.body)) == (400, {"error": message})
    empty = call(ui, "POST", "/api/items/10/art/set", b"", {"Content-Type": "image/png"})
    assert empty.status == 400
    bad = job(ui, "/api/items/10/art/set", b"garbage", {"Content-Type": "image/png"})
    assert bad["state"] == "failed"
    assert bad["error"].startswith("not a readable image")


def test_art_set_reports_download_failures(ui: WebUi, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(method: str, url: str, **kwargs: Any) -> bytes:
        raise http.RequestError("ConnectionError", url)

    monkeypatch.setattr(http, "request", broken)
    found = job(ui, "/api/items/10/art/set", {"url": "https://example.com/a.jpg"})
    assert found["error"] == "could not download the image: ConnectionError for https://example.com/a.jpg"
    assert ui.service is not None

    def missing(path: str) -> Image.Image:
        raise http.RequestError("HTTP 404", f"https://image.tmdb.org/t/p/original{path}")

    ui.service.worker.ctx.sources.fetch = missing
    found = job(ui, "/api/items/10/art/set", {"candidate": "/gone.jpg"})
    assert found["error"] == "HTTP 404 for https://image.tmdb.org/t/p/original/gone.jpg"


def test_forget_and_process(ui: WebUi) -> None:
    store = store_of(ui)
    store.manual("10", "poster", "The Matrix")
    assert done(ui, "/api/items/10/process") == {"outcome": "preview"}
    store.manual("10", "poster", "The Matrix")
    found = job(ui, "/api/items/10/forget")
    assert (found["label"], found["result"]) == ("Forget The Matrix (1999)", {"outcome": "preview"})
    assert store.get("10", "poster") is not None
    gone = job(ui, "/api/items/999/process")
    assert (gone["label"], gone["result"]) == ("Process 999", {"outcome": "gone"})
    assert done(ui, "/api/items/999/forget") == {"outcome": "gone"}


def test_a_process_job_drops_the_cached_server_poster(ui: WebUi, library: Library) -> None:
    image_of(call(ui, "GET", "/api/items/10/image?source=server"))
    library.poster = jpeg(size=(300, 450))
    assert image_of(call(ui, "GET", "/api/items/10/image?source=server")).size == (600, 900)
    done(ui, "/api/items/10/process")
    assert image_of(call(ui, "GET", "/api/items/10/image?source=server")).size == (300, 450)


def test_tmdb_preview(ui: WebUi) -> None:
    found = job(ui, "/api/tools/tmdb-preview", {"kind": "tv", "id": "603", "seasons": [3, 1, 1]})
    assert found["label"] == "Preview TMDB tv 603 seasons 1, 3"
    renders = found["result"]["renders"]
    assert [(r["target"], r["name"]) for r in renders] == [
        ("poster", "Example Movie"), ("art", "Example Movie"),
        ("poster", "Example Movie · Season 1"), ("poster", "Example Movie · Season 3"),
    ]  # fmt: skip
    movie = done(ui, "/api/tools/tmdb-preview", {"kind": "movie", "id": 603, "seasons": [1]})
    assert [r["target"] for r in movie["renders"]] == ["poster", "art"]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "book", "id": 1},
        {"kind": "movie", "id": 0},
        {"kind": "movie", "id": True},
        {"kind": "movie", "id": "x"},
        {"kind": "tv", "id": 1, "seasons": "1"},
        {"kind": "tv", "id": 1, "seasons": [True]},
        {"kind": "tv", "id": 1, "seasons": list(range(61))},
    ],
)
def test_tmdb_preview_refuses_bad_requests(ui: WebUi, body: dict[str, Any]) -> None:
    assert call(ui, "POST", "/api/tools/tmdb-preview", body).status == 400


def test_tmdb_preview_failure(ui: WebUi) -> None:
    assert ui.service is not None
    tmdb = ui.service.worker.ctx.sources.tmdb
    assert isinstance(tmdb, FakeTmdb)
    tmdb.posters, tmdb.backdrops = [], []
    found = job(ui, "/api/tools/tmdb-preview", {"kind": "movie", "id": 5})
    assert (found["state"], found["error"]) == ("failed", "TMDB has no textless art for Example Movie")


def test_routes_need_a_session(ui: WebUi) -> None:
    response = ui.dispatch(Request("GET", "/api/items", {}, _Headers({})))
    assert response.status == 401
