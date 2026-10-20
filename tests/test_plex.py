import hashlib
import json
import urllib.parse
from collections.abc import Callable
from typing import Any

import pytest

from posteryard import http
from posteryard import plex as plex_module
from posteryard.plex import Plex
from posteryard.server import external_ids, labels, tmdb_id

BASE = "http://plex.example:32400"


class Server:
    """Answers Plex requests from a table of path -> JSON, and records every call."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, url: str, *, headers: dict[str, str], redirects: bool, **kwargs: Any) -> bytes:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        assert headers["X-Plex-Token"] == "example-token"
        assert "example-token" not in url
        assert not redirects
        self.calls.append((method, parts.path, query))
        answer = self.routes.get(f"{method} {parts.path}", self.routes.get(parts.path))
        if callable(answer):
            answer = answer(query)
        if answer is None:
            raise http.HttpError(404, url)
        return answer if isinstance(answer, bytes) else json.dumps({"MediaContainer": answer}).encode()


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], tuple[Plex, Server]]:
    def start(routes: dict[str, Any]) -> tuple[Plex, Server]:
        server = Server(routes)
        monkeypatch.setattr(http, "request", server)
        return Plex(BASE, "example-token"), server

    return start


def test_items_children_and_a_missing_item(serve: Any) -> None:
    plex, _ = serve(
        {
            "/library/sections": {"Directory": [{"key": "3", "title": "Movies"}]},
            "/library/metadata/1": {"Metadata": [{"ratingKey": "1"}]},
            "/library/metadata/1/children": {"Metadata": [{"ratingKey": "2"}]},
        }
    )
    assert plex.sections() == [{"key": "3", "title": "Movies"}]
    assert plex.item("1") == {"ratingKey": "1"}
    assert plex.item("9") is None
    assert plex.item("0123456789abcdef0123456789abcdef") is None
    assert plex.children("1") == [{"ratingKey": "2"}]


def test_section_items_reads_every_page(serve: Any) -> None:
    def page(query: dict[str, str]) -> dict[str, Any]:
        start = int(query["X-Plex-Container-Start"])
        keys = list(range(start, min(start + 200, 450)))
        return {"totalSize": 450, "Metadata": [{"ratingKey": str(k)} for k in keys]}

    plex, server = serve({"/library/sections/3/all": page})
    assert len(list(plex.section_items("3", "movie", label="x"))) == 450
    assert [c[2]["X-Plex-Container-Start"] for c in server.calls] == ["0", "200", "400"]
    assert server.calls[0][2]["label"] == "x"


def test_changed_since_merges_added_and_updated(serve: Any) -> None:
    def changed(query: dict[str, str]) -> dict[str, Any]:
        keys = ["1", "2"] if "addedAt>>" in query else ["2", "3"]
        return {"totalSize": 2, "Metadata": [{"ratingKey": k} for k in keys]}

    plex, _ = serve({"/library/sections/3/all": changed})
    assert sorted(i["ratingKey"] for i in plex.changed_since("3", "show", 100)) == ["1", "2", "3"]


def test_upload_selects_the_new_image(serve: Any) -> None:
    listings = iter([[{"ratingKey": "old", "selected": True}], [{"ratingKey": "old"}, {"ratingKey": "new"}]])
    plex, server = serve(
        {
            "GET /library/metadata/1/posters": lambda _q: {"Metadata": next(listings)},
            "POST /library/metadata/1/posters": b"",
            "PUT /library/metadata/1/poster": b"",
        }
    )
    assert plex.upload("1", "poster", b"jpeg") == "new"
    assert server.calls[-1] == ("PUT", "/library/metadata/1/poster", {"url": "new"})


def test_upload_of_bytes_uploaded_before_selects_their_image(serve: Any) -> None:
    earlier = "upload://posters/" + hashlib.sha1(b"jpeg").hexdigest()
    listing = [{"ratingKey": earlier}, {"ratingKey": "upload://posters/other", "selected": True}]
    plex, server = serve(
        {
            "GET /library/metadata/1/posters": {"Metadata": listing},
            "POST /library/metadata/1/posters": b"",
            "PUT /library/metadata/1/poster": b"",
        }
    )
    assert plex.upload("1", "poster", b"jpeg") == earlier
    assert server.calls[-1] == ("PUT", "/library/metadata/1/poster", {"url": earlier})


def test_upload_that_leaves_nothing_fails(serve: Any) -> None:
    plex, _ = serve({"GET /library/metadata/1/arts": {"Metadata": []}, "POST /library/metadata/1/arts": b""})
    with pytest.raises(http.RequestError, match="no art"):
        plex.upload("1", "art", b"jpeg")


def test_selected_poster_bytes_label_and_lock(serve: Any) -> None:
    plex, server = serve(
        {
            "/library/metadata/1/posters": {"Metadata": [{"ratingKey": "a"}, {"ratingKey": "b", "selected": True}]},
            "/library/metadata/1/thumb/5": b"image",
            "PUT /library/sections/3/all": b"",
        }
    )
    item = {"ratingKey": "1", "type": "movie", "librarySectionID": 3, "thumb": "/library/metadata/1/thumb/5"}
    assert plex.selected("1", "poster") == "b"
    assert plex.poster_bytes(item) == b"image"
    with pytest.raises(http.RequestError, match="no poster"):
        plex.poster_bytes({**item, "thumb": "https://elsewhere.example/a.jpg"})
    plex.remove_label(item, "posteryard-next")
    plex.lock(item, "art")
    assert server.calls[-2][2] == {"type": "1", "id": "1", "label[].tag.tag-": "posteryard-next"}
    assert server.calls[-1][2] == {"type": "1", "id": "1", "art.locked": "1"}


def test_guids_and_labels() -> None:
    assert tmdb_id({"Guid": [{"id": "imdb://tt1"}, {"id": "tmdb://42"}]}) == 42
    assert tmdb_id({}) is None
    assert tmdb_id({"guid": "com.plexapp.agents.themoviedb://603?lang=en"}) == 603
    assert tmdb_id({"Guid": [{"id": "tmdb://"}]}) is None
    assert external_ids({"guid": "com.plexapp.agents.thetvdb://121361/1/2?lang=en"}) == {"tvdb": "121361"}
    assert external_ids({"guid": "com.plexapp.agents.hama://anidb-1?lang=en"}) == {}
    assert external_ids({"Guid": [{"id": "imdb://tt1"}, {"id": "tvdb://5"}]}) == {"imdb": "tt1", "tvdb": "5"}
    assert labels({"Label": [{"tag": "Posteryard-Next"}]}) == {"posteryard-next"}


def test_restore_selects_the_agents_image_and_unlocks(serve: Any) -> None:
    images = [{"ratingKey": "upload://posters/1"}, {"ratingKey": "metadata://posters/agent"}]
    plex, server = serve(
        {
            "GET /library/metadata/7/posters": {"Metadata": images},
            "PUT /library/metadata/7/poster": b"",
            "PUT /library/sections/4/all": b"",
        }
    )
    plex.restore({"ratingKey": "7", "type": "episode", "librarySectionID": 4}, "thumb")
    puts = [call for call in server.calls if call[0] == "PUT"]
    assert puts[0][2]["thumb.locked"] == "0"
    assert puts[1] == ("PUT", "/library/metadata/7/poster", {"url": "metadata://posters/agent"})


def test_newest_added(serve: Any) -> None:
    plex, server = serve({"/library/sections/4/all": lambda q: {"Metadata": [{"addedAt": 1700000000}]}})
    assert plex.newest_added("4", "episode", **{"show.id": "9"}) == 1700000000
    assert server.calls[-1][2]["sort"] == "addedAt:desc"
    assert server.calls[-1][2]["show.id"] == "9"


def test_collection_calls(serve: Any) -> None:
    plex, server = serve(
        {
            "/identity": {"machineIdentifier": "abc"},
            "POST /library/collections": {"Metadata": [{"ratingKey": "60"}]},
            "PUT /library/collections/60/items": b"",
            "DELETE /library/collections/60/items/2": b"",
            "DELETE /library/collections/60": b"",
            "PUT /library/sections/4/all": b"",
            "/library/collections/60/children": {"Metadata": [{"ratingKey": "1"}]},
        }
    )
    assert plex.create_collection("4", "show", "Netflix", ["1", "2"]) == "60"
    created = server.calls[-1][2]
    assert created["uri"] == "server://abc/com.plexapp.plugins.library/library/metadata/1,2"
    assert (created["type"], created["sectionId"], created["title"]) == ("2", "4", "Netflix")
    plex.add_to_collection("60", ["3"])
    plex.remove_from_collection("60", "2")
    plex.set_label("4", "collection", "60", "posteryard-collection")
    assert server.calls[-1][2]["type"] == "18"
    assert [c["ratingKey"] for c in plex.collection_children("60")] == ["1"]
    plex.delete_collection("60")
    assert plex.server_id() == "plex:abc"
    assert [c[:2] for c in server.calls].count(("GET", "/identity")) == 1


GUIDE = "tv.plex.providers.epg.xmltv:15"
SHOW = "tv%2Eplex%2Exmltv%3A%2F%2Fshow%2FNews"


def test_guides_keep_only_plex_guide_providers(serve: Any) -> None:
    plex, _ = serve(
        {
            "/livetv/dvrs": {
                "Dvr": [
                    {"epgIdentifier": GUIDE, "refreshedAt": 1700000000},
                    {"epgIdentifier": "../../library", "refreshedAt": 1},
                ]
            }
        }
    )
    assert [(g.provider, g.refreshed_at) for g in plex.guides()] == [(GUIDE, 1700000000)]


def test_guide_airings_read_every_page(serve: Any) -> None:
    pages = iter(
        [{"totalSize": 3, "Metadata": [{"ratingKey": "a"}, {"ratingKey": "b"}]}, {"Metadata": [{"ratingKey": "c"}]}]
    )
    plex, server = serve({f"/{GUIDE}/grid": lambda _q: next(pages)})
    assert [a["ratingKey"] for a in plex.guide_airings(GUIDE, 100, 200)] == ["a", "b", "c"]
    assert server.calls[1][2]["X-Plex-Container-Start"] == "2"
    assert server.calls[0][2]["endsAt>"] == "100"


def test_guide_upload_selects_the_upload_of_these_bytes(serve: Any) -> None:
    digest = "upload://posters/" + hashlib.sha1(b"jpeg").hexdigest()
    plex, server = serve(
        {
            f"GET /{GUIDE}/metadata/{SHOW}": {"Metadata": [{"guid": "g"}]},
            f"GET /{GUIDE}/metadata/{SHOW}/posters": {"Metadata": [{"ratingKey": digest, "selected": True}]},
            f"POST /{GUIDE}/metadata/{SHOW}/posters": b"",
            f"PUT /{GUIDE}/metadata/{SHOW}/poster": b"",
        }
    )
    assert plex.guide_item(GUIDE, SHOW) == {"guid": "g"}
    assert plex.guide_upload(GUIDE, SHOW, b"jpeg") == digest
    assert server.calls[-1] == ("PUT", f"/{GUIDE}/metadata/{SHOW}/poster", {"url": digest})


def test_guide_upload_that_leaves_nothing_fails(serve: Any) -> None:
    plex, _ = serve({f"GET /{GUIDE}/metadata/{SHOW}/posters": {}, f"POST /{GUIDE}/metadata/{SHOW}/posters": b""})
    with pytest.raises(http.RequestError, match="no poster"):
        plex.guide_upload(GUIDE, SHOW, b"jpeg")


@pytest.mark.parametrize(("provider", "key"), [("library", SHOW), (GUIDE, "../x"), (GUIDE, "a/b")])
def test_guide_calls_refuse_other_paths(serve: Any, provider: str, key: str) -> None:
    plex, server = serve({})
    with pytest.raises(ValueError, match="not a Plex guide"):
        plex.guide_select(provider, key, "upload://posters/x")
    assert server.calls == []


def guide_by_id(present: dict[int, str]) -> tuple[Plex, list[str]]:
    plex, asked = Plex(BASE, "example-token"), []

    def get(path: str, **_: Any) -> dict[str, Any]:
        asked.append(path)
        if path.endswith("/grid"):
            return {"Metadata": []}
        last = path.rsplit("/", 1)[1]
        ids = [int(n) for n in last.split(",")] if last[0].isdigit() else []
        guid = present.get(ids[0]) if len(ids) == 1 else None
        if path.endswith(f"/{SHOW}"):
            guid = "g/news-at-ten"
        items = [{"guid": present[n]} for n in ids if n in present] if guid is None else [{"guid": guid}]
        if not items:
            raise http.HttpError(404, path)
        return {"Metadata": items}

    plex._get = get  # type: ignore[method-assign]
    return plex, asked


def test_guide_target_is_the_name_key_when_it_reaches_the_entry() -> None:
    plex, asked = guide_by_id({})
    assert plex.guide_target(GUIDE, SHOW, "g/news-at-ten") == SHOW
    assert len(asked) == 1


def test_guide_target_finds_the_numeric_id_when_the_name_key_reaches_another_entry() -> None:
    present = {n: f"g/{n}" for n in range(1, 700) if n % 7}
    present[452] = "g/news"
    plex, asked = guide_by_id(present)
    assert plex.guide_target(GUIDE, SHOW, "g/news") == "452"
    assert len(asked) < 15


def test_guide_target_is_none_when_no_entry_has_the_guid() -> None:
    plex, asked = guide_by_id({n: f"g/{n}" for n in range(1, 400)})
    assert plex.guide_target(GUIDE, SHOW, "g/news") is None
    assert len(asked) == 4
    assert asked[-1].endswith(",900")


def test_guide_ids_are_read_once_per_guide_read() -> None:
    present = {n: f"g/{n}" for n in range(1, 700)}
    plex, asked = guide_by_id(present)
    assert plex.guide_target(GUIDE, SHOW, "g/450") == "450"
    asked.clear()
    assert plex.guide_target(GUIDE, SHOW, "g/100") == "100"
    assert len(asked) == 10
    plex.guide_airings(GUIDE, 0, 1)
    asked.clear()
    assert plex.guide_target(GUIDE, SHOW, "g/100") == "100"
    assert len(asked) == 14


def test_guide_ids_past_the_limit_are_not_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(plex_module, "GUIDE_ID_BATCHES", 2)
    plex, asked = guide_by_id({n: f"g/{n}" for n in range(1, 1000)})
    assert plex.guide_target(GUIDE, SHOW, "g/900") is None
    assert len(asked) == 3
    assert "more entries than are read" in caplog.text


def test_guide_ids_are_not_read_again_after_an_error(caplog: pytest.LogCaptureFixture) -> None:
    plex, asked = guide_by_id({n: f"g/{n}" for n in range(1, 700)})
    get = plex._get

    def failing(path: str, **params: Any) -> Any:
        if path.endswith(",600"):
            asked.append(path)
            raise http.HttpError(500, path)
        return get(path, **params)

    plex._get = failing  # type: ignore[method-assign]
    assert plex.guide_target(GUIDE, SHOW, "g/100") == "100"
    asked.clear()
    assert plex.guide_target(GUIDE, SHOW, "g/450") is None
    assert len(asked) == 1
    assert "could not all be read" in caplog.text


def test_guide_target_is_none_for_an_entry_without_a_guid() -> None:
    plex, asked = guide_by_id({1: ""})
    assert plex.guide_target(GUIDE, SHOW, "") is None
    assert asked == []


def test_image_bytes_reads_the_shown_poster_background_or_thumbnail(serve: Any) -> None:
    plex, _ = serve({"/library/metadata/1/thumb/5": b"poster", "/library/metadata/1/art/6": b"background"})
    item = {"ratingKey": "1", "thumb": "/library/metadata/1/thumb/5", "art": "/library/metadata/1/art/6"}
    assert plex.image_bytes(item, "poster") == b"poster"
    assert plex.image_bytes(item, "thumb") == b"poster"
    assert plex.image_bytes(item, "art") == b"background"
    with pytest.raises(http.HttpError) as caught:
        plex.image_bytes({"ratingKey": "1"}, "art")
    assert caught.value.status == 404
