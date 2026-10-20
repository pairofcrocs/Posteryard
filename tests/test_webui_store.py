import time
from pathlib import Path

import pytest

from posteryard.overrides import Override
from posteryard.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def stamp(store: Store, key: str, target: str, when: int) -> None:
    store._db.execute("UPDATE images SET updated_at=? WHERE rating_key=? AND target=?", (when, key, target))


def test_records_newest_first_with_filters_and_paging(store: Store) -> None:
    store.uploaded("1", "poster", "One", "fp", "img")
    store.previewed("1", "art", "One", "fp")
    store.failed("2", "poster", "Two", "boom")
    store.manual("3", "poster", "Three")
    for when, (key, target) in enumerate([("1", "poster"), ("1", "art"), ("2", "poster"), ("3", "poster")]):
        stamp(store, key, target, 1000 + when)
    every = store.records()
    assert [(r.rating_key, r.target) for r in every] == [
        ("3", "poster"),
        ("2", "poster"),
        ("1", "art"),
        ("1", "poster"),
    ]
    assert every[1].title == "Two" and every[1].status == "failed" and every[1].failures == 1
    assert [r.rating_key for r in store.records(status="failed")] == ["2"]
    assert [r.target for r in store.records(rating_key="1")] == ["art", "poster"]
    assert [r.target for r in store.records(rating_key="1", status="uploaded")] == ["poster"]
    assert [r.rating_key for r in store.records(limit=2, offset=1)] == ["2", "1"]
    assert store.records(status="uploading") == []


def test_records_with_the_same_time_keep_a_stable_order(store: Store) -> None:
    for key in ("9", "10", "11"):
        store.previewed(key, "poster", key, "fp")
        stamp(store, key, "poster", 5)
    assert [r.rating_key for r in store.records()] == ["10", "11", "9"]


def test_count(store: Store) -> None:
    assert store.count() == 0 and store.count("failed") == 0
    store.failed("1", "poster", "One", "boom")
    store.failed("1", "item", "One", "boom")
    store.uploaded("2", "poster", "Two", "fp", "img")
    assert store.count() == 3 and store.count("failed") == 2 and store.count("uploaded") == 1


def test_last_error(store: Store) -> None:
    assert store.last_error("1", "poster") == ""
    store.failed("1", "poster", "One", "TMDB has no poster")
    assert store.last_error("1", "poster") == "TMDB has no poster"
    assert store.last_error("1", "art") == ""


def test_statuses_by_target(store: Store) -> None:
    store.uploaded("1", "poster", "One", "fp", "img")
    store.failed("2", "poster", "Two", "boom")
    store.previewed("1", "art", "One", "fp")
    store.manual("3", "thumb", "Three")
    assert store.statuses() == {"1": "uploaded", "2": "failed"}
    assert store.statuses("art") == {"1": "preview"}
    assert store.statuses("thumb") == {"3": "manual"}


def test_all_overrides(store: Store) -> None:
    assert store.all_overrides() == {}
    store.set_custom("1", "https://example.com/art.jpg", "command")
    store.add_skip("2", "/a.jpg")
    store.add_skip("2", "/b.jpg")
    assert store.all_overrides() == {
        "1": Override(custom="https://example.com/art.jpg", source="command", skip=frozenset()),
        "2": Override(custom=None, source="", skip=frozenset({"/a.jpg", "/b.jpg"})),
    }


def test_guides_by_title(store: Store) -> None:
    assert store.guides() == []
    before = int(time.time())
    store.guide_uploaded("b", "News", "fp1", "img1", "https://example.com/news.jpg")
    store.guide_uploaded("a", "Cartoons", "fp2", "img2", "")
    guides = store.guides()
    assert [g.key for g in guides] == ["a", "b"]
    assert guides[1].title == "News" and guides[1].picture == "https://example.com/news.jpg"
    assert guides[1].fingerprint == "fp1" and guides[1].image_key == "img1" and guides[1].seen_at >= before
