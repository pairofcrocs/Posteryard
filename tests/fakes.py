import hashlib
from collections.abc import Iterable
from datetime import date
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from posteryard import pipeline
from posteryard.config import Config
from posteryard.notify import Notifier
from posteryard.ocr import TextLine
from posteryard.quality import QualityMinimums
from posteryard.server import Item, MediaServer, Target
from posteryard.settings import Settings
from posteryard.sources import Sources
from posteryard.store import Store
from posteryard.tmdb import Details, Episode, ImageRef, Images, Kind, RegionOffers, Tmdb
from posteryard.worker import Worker


def ref(path: str, language: str | None) -> ImageRef:
    return ImageRef(path, language, 2000, 3000, 5.0, 10)


class FakeServer(MediaServer):
    """A media server with no items. Subclasses override what a test needs."""

    name = "Plex"
    url = "http://plex.example:32400"
    collections_per_library = True

    def server_id(self) -> str:
        return "plex:example"

    def sections(self) -> list[Item]:
        return []

    def item(self, rating_key: str) -> Item | None:
        return None

    def children(self, rating_key: str) -> list[Item]:
        return []

    def section_items(self, section_key: str, kind: str, **filters: Any) -> Iterable[Item]:
        return []

    def changed_since(self, section_key: str, kind: str, since: int) -> list[Item]:
        return []

    def newest_added(self, section_key: str, kind: str, **filters: Any) -> int | None:
        return None

    def selected(self, rating_key: str, target: Target) -> str | None:
        return None

    def upload(self, rating_key: str, target: Target, jpeg: bytes) -> str:
        return f"upload://posters/{rating_key}-{target}"

    def poster_bytes(self, item: Item) -> bytes:
        return b""

    def image_bytes(self, item: Item, target: Target) -> bytes:
        return self.poster_bytes(item) if target == "poster" else b""

    def remove_label(self, item: Item, label: str) -> None:
        pass

    def lock(self, item: Item, target: Target) -> None:
        pass

    def restore(self, item: Item, target: Target) -> None:
        pass

    def collection_children(self, rating_key: str) -> list[Item]:
        return []

    def create_collection(self, section_key: str, kind: str, title: str, rating_keys: list[str]) -> str:
        return "60"

    def add_to_collection(self, rating_key: str, members: list[str]) -> None:
        pass

    def remove_from_collection(self, rating_key: str, member: str) -> None:
        pass

    def delete_collection(self, rating_key: str) -> None:
        pass

    def set_label(self, section_key: str, kind: str, rating_key: str, label: str) -> None:
        pass


class FakeTmdb(Tmdb):
    """A show with seasons 1 to 4. Seasons 1 and 3 have their own textless art."""

    def __init__(self, posters: list[ImageRef] | None = None, seasons: int = 4) -> None:
        super().__init__("example")
        self.posters = posters or []
        self.seasons = seasons
        self.backdrops = [
            ImageRef("/backdrop.jpg", None, 3840, 2160, 5, 10),
            ImageRef("/backdrop2.jpg", None, 3840, 2160, 4, 1),
        ]
        self.logos = [ref("/logo.png", "en")]
        self.finds: list[str] = []

    def details(self, kind: Kind, tmdb_id: int) -> Details:
        return {"title": "Example Movie", "seasons": [{"season_number": n} for n in range(1, self.seasons + 1)]}

    def images(self, kind: Kind, tmdb_id: int) -> Images:
        return Images(self.posters, self.backdrops, self.logos)

    def season_images(self, show_id: int, season: int) -> Images:
        return Images([ref(f"/season{season}.jpg", None)] if season in (1, 3) else [], [], [])

    def all_titles(self, kind: Kind, tmdb_id: int) -> list[str]:
        return ["Example Movie", "Película de Ejemplo"]

    def watch_providers(self, kind: Kind, tmdb_id: int) -> dict[str, RegionOffers]:
        return {}

    def episode(self, show_id: int, season: int, episode: int) -> Episode | None:
        return {"still_path": f"/still-{season}-{episode}.jpg"}

    def find(self, source: str, external_id: str) -> dict[str, int]:
        self.finds.append(external_id)
        return {"movie": 77} if external_id == "tt0000077" else {}


def worker(cfg: Config, server: MediaServer, store: Store, notifier: Notifier | None = None) -> Worker:
    return Worker(cfg, server, store, notifier or Notifier(), FakeTmdb())


TEXT = {"/foreign.jpg": "PELICULA DE EJEMPLO", "/english.jpg": "EXAMPLE MOVIE"}


def fetch(path: str) -> Image.Image:
    if path.endswith(".png"):
        logo = Image.new("RGBA", (600, 120), (0, 0, 0, 0))
        ImageDraw.Draw(logo).rectangle((0, 0, 599, 119), fill=(255, 255, 255, 255))
        return logo
    noise = np.random.default_rng(int(hashlib.sha256(path.encode()).hexdigest()[:8], 16)).integers(0, 160, (8, 9))
    image = Image.fromarray(noise.astype(np.uint8)).convert("RGB").resize((200, 300), Image.Resampling.NEAREST)
    image.info["path"] = path
    return image


def read(image: Image.Image) -> list[TextLine]:
    text = TEXT.get(str(image.info.get("path")), "")
    return [TextLine(text, 0.99, 0.06, 0.6)] if text else []


def context(posters: list[ImageRef], seasons: int = 4) -> pipeline.Context:
    settings = Settings(QualityMinimums(), ("CA",), {"1": date(2026, 10, 4)}, date(2026, 10, 1))
    return pipeline.Context(settings, Sources(FakeTmdb(posters, seasons), settings, read=read, fetch=fetch))


def tmdb_of(ctx: pipeline.Context) -> FakeTmdb:
    assert isinstance(ctx.sources.tmdb, FakeTmdb)
    return ctx.sources.tmdb
