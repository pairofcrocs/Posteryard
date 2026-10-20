"""Plex, Jellyfin and Emby all answer with the Plex-shaped `Item`."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict


class Item(TypedDict, total=False):
    ratingKey: str
    type: str
    title: str
    index: int
    parentIndex: int
    parentRatingKey: str
    grandparentRatingKey: str
    Guid: list[dict[str, str]]
    guid: str
    addedAt: int | None
    updatedAt: int
    Label: list[dict[str, str]]
    librarySectionID: int | str
    librarySectionTitle: str
    Media: list[dict[str, Any]]
    thumb: str
    key: str
    year: int
    paths: list[str]


Target = Literal["poster", "art", "thumb"]
TARGETS: frozenset[str] = frozenset({"poster", "art", "thumb"})
ITEM_KEY = re.compile(r"^(\d{1,12}|[0-9a-f]{32})$")
SOURCES = ("tmdb", "imdb", "tvdb")
LEGACY_AGENTS = {"themoviedb": "tmdb", "imdb": "imdb", "thetvdb": "tvdb"}
LEGACY_GUID = re.compile(r"^com\.plexapp\.agents\.(\w+)://([^/?]+)")


def is_item_key(text: str) -> bool:
    return bool(ITEM_KEY.match(text))


class MediaServer(Protocol):
    url: str
    name: str
    collections_per_library: bool

    def server_id(self) -> str: ...
    def sections(self) -> list[Item]: ...
    def item(self, rating_key: str) -> Item | None: ...
    def children(self, rating_key: str) -> list[Item]: ...
    def section_items(self, section_key: str, kind: str, **filters: Any) -> Iterable[Item]: ...
    def changed_since(self, section_key: str, kind: str, since: int) -> list[Item]: ...
    def newest_added(self, section_key: str, kind: str, **filters: Any) -> int | None: ...
    def selected(self, rating_key: str, target: Target) -> str | None: ...
    def upload(self, rating_key: str, target: Target, jpeg: bytes) -> str: ...
    def poster_bytes(self, item: Item) -> bytes: ...
    def image_bytes(self, item: Item, target: Target) -> bytes: ...
    def remove_label(self, item: Item, label: str) -> None: ...
    def lock(self, item: Item, target: Target) -> None: ...
    def restore(self, item: Item, target: Target) -> None: ...
    def collection_children(self, rating_key: str) -> list[Item]: ...
    def create_collection(self, section_key: str, kind: str, title: str, rating_keys: list[str]) -> str: ...
    def add_to_collection(self, rating_key: str, members: list[str]) -> None: ...
    def remove_from_collection(self, rating_key: str, member: str) -> None: ...
    def delete_collection(self, rating_key: str) -> None: ...
    def set_label(self, section_key: str, kind: str, rating_key: str, label: str) -> None: ...


@dataclass(frozen=True)
class Guide:
    provider: str
    refreshed_at: int


class GuideServer(Protocol):
    """A server with a Live TV guide Posteryard can put posters on. Airings come back Plex-shaped."""

    name: str

    def guides(self) -> list[Guide]: ...
    def guide_airings(self, provider: str, start: int, end: int) -> list[Item]: ...
    def guide_target(self, provider: str, key: str, guid: str) -> str | None: ...
    def guide_images(self, provider: str, key: str) -> list[Item]: ...
    def guide_upload(self, provider: str, key: str, jpeg: bytes) -> str: ...
    def guide_select(self, provider: str, key: str, image_key: str) -> None: ...


def labels(item: Item) -> set[str]:
    return {str(label.get("tag", "")).lower() for label in item.get("Label") or []}


def label_tag(item: Item, label: str) -> str | None:
    return next((str(t["tag"]) for t in item.get("Label") or [] if str(t.get("tag", "")).lower() == label), None)


def external_ids(item: Item) -> dict[str, str]:
    ids: dict[str, str] = {}
    for guid in item.get("Guid") or []:
        source, _, value = str(guid.get("id", "")).partition("://")
        if source in SOURCES and value:
            ids.setdefault(source, value)
    legacy = LEGACY_GUID.match(str(item.get("guid") or ""))
    if legacy and legacy.group(1) in LEGACY_AGENTS:
        ids.setdefault(LEGACY_AGENTS[legacy.group(1)], legacy.group(2))
    return ids


def tmdb_id(item: Item) -> int | None:
    value = external_ids(item).get("tmdb", "")
    return int(value) if value.isascii() and value.isdigit() else None
