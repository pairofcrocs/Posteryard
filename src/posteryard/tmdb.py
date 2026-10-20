import io
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from PIL import Image

from posteryard import http

Kind = Literal["movie", "tv"]
API = "https://api.themoviedb.org/3"
IMAGES = "https://image.tmdb.org/t/p"
MAX_SIDE = 2160
# Larger images are refused before decoding. Decoding one costs about 3 bytes per pixel.
MAX_PIXELS = 50_000_000
FALLBACK_SIZE = "w1280"
CACHE_SECONDS = 600
CACHE_SIZE = 64
TITLE_PARTS = "images,alternative_titles,watch/providers,external_ids"


class RawImage(TypedDict, total=False):
    file_path: str
    iso_639_1: str | None
    width: int
    height: int
    vote_average: float
    vote_count: int


class ImageSet(TypedDict, total=False):
    posters: list[RawImage]
    backdrops: list[RawImage]
    logos: list[RawImage]


class Episode(TypedDict, total=False):
    episode_number: int
    air_date: str | None
    still_path: str | None


class SeasonSummary(TypedDict, total=False):
    season_number: int


class Season(TypedDict, total=False):
    episodes: list[Episode]
    images: ImageSet


class ExternalIds(TypedDict, total=False):
    tvdb_id: int | None
    wikidata_id: str | None


class AlternativeTitle(TypedDict, total=False):
    title: str


class AlternativeTitles(TypedDict, total=False):
    titles: list[AlternativeTitle]
    results: list[AlternativeTitle]


class Provider(TypedDict, total=False):
    provider_id: int
    provider_name: str
    logo_path: str | None
    display_priority: int


class RegionOffers(TypedDict, total=False):
    link: str
    flatrate: list[Provider]
    free: list[Provider]
    ads: list[Provider]
    buy: list[Provider]
    rent: list[Provider]


class WatchProviders(TypedDict, total=False):
    results: dict[str, RegionOffers]


class CollectionRef(TypedDict, total=False):
    id: int
    name: str
    poster_path: str | None


class CollectionPart(TypedDict, total=False):
    id: int
    title: str
    release_date: str | None
    poster_path: str | None


class Collection(TypedDict, total=False):
    id: int
    name: str
    poster_path: str | None
    parts: list[CollectionPart]


Details = TypedDict(
    "Details",
    {
        "title": str,
        "name": str,
        "original_title": str,
        "original_name": str,
        "original_language": str,
        "origin_country": list[str],
        "seasons": list[SeasonSummary],
        "next_episode_to_air": Episode | None,
        "images": ImageSet,
        "alternative_titles": AlternativeTitles,
        "external_ids": ExternalIds,
        "watch/providers": WatchProviders,
        "belongs_to_collection": CollectionRef | None,
    },
    total=False,
)


@dataclass(frozen=True)
class ImageRef:
    path: str
    language: str | None
    width: int
    height: int
    votes: float
    vote_count: int


def _refs(raw: Sequence[RawImage]) -> list[ImageRef]:
    refs = [
        ImageRef(
            path=str(r["file_path"]),
            language=r.get("iso_639_1") or None,
            width=int(r.get("width") or 0),
            height=int(r.get("height") or 0),
            votes=float(r.get("vote_average") or 0),
            vote_count=int(r.get("vote_count") or 0),
        )
        for r in raw
        if r.get("file_path")
    ]
    return sorted(refs, key=lambda r: (-r.votes, -r.vote_count, -r.width))


@dataclass(frozen=True)
class Images:
    posters: list[ImageRef]
    backdrops: list[ImageRef]
    logos: list[ImageRef]

    def textless_posters(self) -> list[ImageRef]:
        return [r for r in self.posters if r.language in (None, "xx")]

    def textless_backdrops(self) -> list[ImageRef]:
        return [r for r in self.backdrops if r.language in (None, "xx")]

    def textless_art(self) -> list[ImageRef]:
        return self.textless_posters() + self.textless_backdrops()

    def logos_in(self, languages: Sequence[str]) -> list[ImageRef]:
        for language in languages:
            found = [r for r in self.logos if r.language == language and r.path.endswith(".png")]
            if found:
                return found
        return []


class Tmdb:
    def __init__(self, api_key: str, languages: Sequence[str] = ("en",)) -> None:
        self.api_key = api_key
        self.languages = tuple(languages)
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def _image_languages(self) -> str:
        return ",".join(dict.fromkeys([*self.languages, "en", "null", "xx"]))

    @property
    def _is_token(self) -> bool:
        return self.api_key.count(".") == 2

    def _get(self, path: str, **params: Any) -> Any:
        if self._is_token:
            url = f"{API}{path}?{urllib.parse.urlencode(params)}"
            return http.get_json(url, {"Authorization": f"Bearer {self.api_key}"})
        return http.get_json(
            f"{API}{path}?{urllib.parse.urlencode({**params, 'api_key': self.api_key})}", redirects=False
        )

    def _cached(self, key: str, load: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_SECONDS:
                self._cache.move_to_end(key)
                return hit[1]
        value = load()
        with self._lock:
            self._cache[key] = (now, value)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return value

    def details(self, kind: Kind, tmdb_id: int) -> Details:
        details: Details = self._cached(
            f"{kind}/{tmdb_id}",
            lambda: self._get(
                f"/{kind}/{tmdb_id}", append_to_response=TITLE_PARTS, include_image_language=self._image_languages
            ),
        )
        return details

    def tvdb_id(self, kind: Kind, tmdb_id: int) -> int | None:
        value = (self.details(kind, tmdb_id).get("external_ids") or {}).get("tvdb_id")
        return int(value) if value else None

    def images(self, kind: Kind, tmdb_id: int) -> Images:
        raw = self.details(kind, tmdb_id).get("images") or {}
        return Images(_refs(raw.get("posters", [])), _refs(raw.get("backdrops", [])), _refs(raw.get("logos", [])))

    def _season(self, show_id: int, season: int) -> Season:

        def load() -> Season:
            try:
                data: Season = self._get(
                    f"/tv/{show_id}/season/{season}",
                    append_to_response="images",
                    include_image_language=self._image_languages,
                )
            except http.HttpError as exc:
                if exc.status == 404:
                    return {}
                raise
            return data

        season_data: Season = self._cached(f"tv/{show_id}/season/{season}", load)
        return season_data

    def season_images(self, show_id: int, season: int) -> Images:
        raw = self._season(show_id, season).get("images") or {}
        return Images(_refs(raw.get("posters", [])), [], [])

    def episode(self, show_id: int, season: int, episode: int) -> Episode | None:
        episodes = self._season(show_id, season).get("episodes") or []
        return next((e for e in episodes if e.get("episode_number") == episode), None)

    def all_titles(self, kind: Kind, tmdb_id: int) -> list[str]:
        details = self.details(kind, tmdb_id)
        titles = [str(details.get(k) or "") for k in ("title", "name", "original_title", "original_name")]
        raw = details.get("alternative_titles") or {}
        titles += [str(alt.get("title", "")) for alt in raw.get("titles") or raw.get("results") or []]
        return [t for t in dict.fromkeys(titles) if t]

    def find(self, source: str, external_id: str) -> dict[str, int]:
        raw = self._get(f"/find/{urllib.parse.quote(external_id, safe='')}", external_source=f"{source}_id")
        found: dict[str, int] = {}
        for kind, field in (("movie", "movie_results"), ("tv", "tv_results")):
            results = raw.get(field) or []
            if results and results[0].get("id"):
                found[kind] = int(results[0]["id"])
        return found

    def language_logos(self, kind: Kind, tmdb_id: int, languages: Sequence[str]) -> list[ImageRef]:
        raw = self._cached(
            f"{kind}/{tmdb_id}/logos/{','.join(languages)}",
            lambda: self._get(f"/{kind}/{tmdb_id}/images", include_image_language=",".join(languages)),
        )
        return _refs(raw.get("logos") or [])

    def collection(self, collection_id: int) -> Collection | None:
        """The movies of a TMDB collection, or None when TMDB has no such collection."""

        def load() -> Collection | None:
            try:
                data: Collection = self._get(f"/collection/{collection_id}")
            except http.HttpError as exc:
                if exc.status == 404:
                    return None
                raise
            return data

        found: Collection | None = self._cached(f"collection/{collection_id}", load)
        return found

    def search(self, kind: Kind, query: str) -> list[dict[str, Any]]:
        raw = self._get(f"/search/{kind}", query=query, include_adult="false")
        return [r for r in raw.get("results") or [] if isinstance(r, dict)]

    def watch_providers(self, kind: Kind, tmdb_id: int) -> dict[str, RegionOffers]:
        return (self.details(kind, tmdb_id).get("watch/providers") or {}).get("results", {})

    def network_logo(self, network_id: int) -> str | None:
        """The network's main logo path. None when TMDB has no such network or no logo for it."""
        try:
            raw = self._cached(f"network/{network_id}", lambda: self._get(f"/network/{network_id}"))
        except http.HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return str(raw.get("logo_path") or "") or None

    @staticmethod
    def image(path: str, size: str = "original") -> Image.Image:
        try:
            return open_image(http.request("GET", f"{IMAGES}/{size}{path}", timeout=60))
        except ValueError:
            if size == FALLBACK_SIZE:
                raise
            return open_image(http.request("GET", f"{IMAGES}/{FALLBACK_SIZE}{path}", timeout=60))


def open_image(data: bytes) -> Image.Image:
    """Decoded and shrunk to MAX_SIDE. Full-size TMDB originals reach 70 MB each in memory.

    Raises ValueError for an image over MAX_PIXELS and OSError for data that is not an image.
    """
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        raise ValueError("the image has too many pixels") from None
    if image.width * image.height > MAX_PIXELS:
        raise ValueError(f"the image is too large: {image.width} x {image.height} pixels")
    image.draft("RGB", (MAX_SIDE, MAX_SIDE))
    image.load()
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    return image
