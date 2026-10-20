import io
import json
import urllib.parse
from typing import Any

import pytest
from PIL import Image

from posteryard import http
from posteryard.tmdb import MAX_SIDE, Tmdb


def answer(routes: dict[str, Any]) -> Any:
    def request(method: str, url: str, **kwargs: Any) -> bytes:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if "api.themoviedb.org" in url:
            assert query.pop("api_key") == "example-key"
            assert kwargs["redirects"] is False
        body = routes.get(parts.path.removeprefix("/3"))
        if body is None:
            raise http.HttpError(404, url)
        return body if isinstance(body, bytes) else json.dumps(body).encode()

    return request


def test_images_are_sorted_and_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {
        "posters": [
            {"file_path": "/low.jpg", "iso_639_1": None, "vote_average": 4, "width": 2000},
            {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 6, "width": 2000},
            {"file_path": "/xx.jpg", "iso_639_1": "xx", "vote_average": 5, "width": 2000},
            {"iso_639_1": None},
        ],
        "backdrops": [{"file_path": "/b.jpg", "iso_639_1": None}],
        "logos": [{"file_path": "/l.png", "iso_639_1": "en"}, {"file_path": "/l.svg", "iso_639_1": "en"}],
    }
    monkeypatch.setattr(http, "request", answer({"/movie/1": {"images": raw}}))
    images = Tmdb("example-key").images("movie", 1)
    assert [r.path for r in images.posters] == ["/en.jpg", "/xx.jpg", "/low.jpg"]
    assert [r.path for r in images.textless_art()] == ["/xx.jpg", "/low.jpg", "/b.jpg"]
    assert [r.path for r in images.logos_in(["en"])] == ["/l.png"]


def test_missing_seasons_and_episodes_are_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    season = {"episodes": [{"episode_number": 3, "still_path": "/s.jpg"}], "images": {"posters": []}}
    monkeypatch.setattr(http, "request", answer({"/tv/1/season/2": season}))
    tmdb = Tmdb("example-key")
    assert tmdb.season_images(1, 9).posters == []
    assert tmdb.episode(1, 9, 1) is None
    assert tmdb.episode(1, 2, 4) is None
    assert tmdb.episode(1, 2, 3) == {"episode_number": 3, "still_path": "/s.jpg"}


def test_titles_and_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    show = {
        "name": "Example",
        "original_name": "Ejemplo",
        "alternative_titles": {"results": [{"title": "Example US"}, {"title": "Example"}]},
        "watch/providers": {"results": {"CA": {"flatrate": []}}},
    }
    calls: list[str] = []
    route = answer({"/tv/1": show})

    def request(method: str, url: str, **kwargs: Any) -> bytes:
        calls.append(url)
        return bytes(route(method, url, **kwargs))

    monkeypatch.setattr(http, "request", request)
    tmdb = Tmdb("example-key")
    assert tmdb.all_titles("tv", 1) == ["Example", "Ejemplo", "Example US"]
    assert tmdb.watch_providers("tv", 1) == {"CA": {"flatrate": []}}
    assert tmdb.details("tv", 1)["name"] == "Example"
    assert len(calls) == 1
    assert "append_to_response=images%2Calternative_titles%2Cwatch%2Fproviders" in calls[0]


def test_images_are_shrunk(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (4000, 6000)).save(buffer, "JPEG")
    monkeypatch.setattr(http, "request", answer({"/t/p/original/a.jpg": buffer.getvalue()}))
    assert max(Tmdb.image("/a.jpg").size) <= MAX_SIDE


def test_oversized_originals_fall_back_to_a_smaller_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    huge, small = io.BytesIO(), io.BytesIO()
    Image.new("1", (10000, 6000)).save(huge, "PNG")
    Image.new("RGBA", (1280, 168)).save(small, "PNG")
    routes = {"/t/p/original/logo.png": huge.getvalue(), "/t/p/w1280/logo.png": small.getvalue()}
    monkeypatch.setattr(http, "request", answer(routes))
    assert Tmdb.image("/logo.png").size == (1280, 168)


def test_an_oversized_fallback_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    huge = io.BytesIO()
    Image.new("1", (10000, 6000)).save(huge, "PNG")
    routes = {"/t/p/original/logo.png": huge.getvalue(), "/t/p/w1280/logo.png": huge.getvalue()}
    monkeypatch.setattr(http, "request", answer(routes))
    with pytest.raises(ValueError, match="too large"):
        Tmdb.image("/logo.png")


def test_find_maps_an_imdb_id_to_tmdb_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {"/find/tt0133093": {"movie_results": [{"id": 603}], "tv_results": []}}
    monkeypatch.setattr(http, "request", answer(routes))
    assert Tmdb("example-key").find("imdb", "tt0133093") == {"movie": 603}
    monkeypatch.setattr(http, "request", answer({"/find/0": {}}))
    assert Tmdb("example-key").find("tvdb", "0") == {}


def test_a_read_access_token_is_sent_as_a_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, str]]] = []

    def request(method: str, url: str, *, headers: dict[str, str], **kwargs: Any) -> bytes:
        sent.append((url, headers))
        return b'{"title": "Example"}'

    monkeypatch.setattr(http, "request", request)
    Tmdb("eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJleGFtcGxlIn0.c2lnbmF0dXJl").details("movie", 1)
    url, headers = sent[0]
    assert "api_key" not in url
    assert headers["Authorization"].startswith("Bearer eyJ")


def test_a_collection_lists_its_movies(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = [{"id": 1, "title": "Dune"}, {"id": 2, "title": "Dune: Part Two"}]
    monkeypatch.setattr(http, "request", answer({"/collection/10": {"id": 10, "name": "Dune", "parts": parts}}))
    found = Tmdb("example-key").collection(10)
    assert found is not None
    assert [p["title"] for p in found["parts"]] == ["Dune", "Dune: Part Two"]
    assert Tmdb("example-key").collection(99) is None


def test_a_collection_outage_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(method: str, url: str, **_kwargs: Any) -> bytes:
        raise http.HttpError(500, url)

    monkeypatch.setattr(http, "request", request)
    with pytest.raises(http.HttpError):
        Tmdb("example-key").collection(98)
