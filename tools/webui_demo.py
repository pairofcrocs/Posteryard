"""Run the real service and web UI against a made-up library, without Plex or TMDB.

    PYTHONPATH=. uv run python tools/webui_demo.py [--port 8000] [--data /tmp/posteryard-demo] [--live]

Sign in with the password `demo`. Art is drawn locally, and text is never found on it.
"""

import argparse
import colorsys
import hashlib
import io
import os
import random
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from posteryard import config, logfmt
from posteryard.notify import Notifier
from posteryard.ocr import TextLine
from posteryard.server import Item, Target
from posteryard.service import Service
from posteryard.store import Store
from posteryard.tmdb import Details, Episode, ImageRef, Images, Kind, RegionOffers
from posteryard.worker import Worker
from tests.fakes import FakeServer, FakeTmdb

MOVIES = [
    ("Arrival", 2016), ("Blade Runner 2049", 2017), ("Dune", 1984), ("Dune", 2021), ("Dune: Part Two", 2024),
    ("Everything Everywhere All at Once", 2022), ("Ex Machina", 2015), ("Interstellar", 2014), ("Her", 2013),
    ("John Wick: Chapter 4", 2023), ("Mad Max: Fury Road", 2015), ("Oppenheimer", 2023), ("Parasite", 2019),
    ("Past Lives", 2023), ("Poor Things", 2023), ("Spider-Man: Across the Spider-Verse", 2023),
    ("Superman", 2025), ("The Grand Budapest Hotel", 2014), ("The Matrix", 1999), ("Thunderbolts*", 2025),
    ("Top Gun: Maverick", 2022), ("Whiplash", 2014), ("1917", 2019), ("Amélie", 2001),
]  # fmt: skip
SHOWS = [
    ("Severance", 2022, 2), ("The Office", 2005, 4), ("Andor", 2022, 2), ("The Bear", 2022, 3),
    ("Shogun", 2024, 1), ("Slow Horses", 2022, 4), ("The Last of Us", 2023, 2), ("Fallout", 2024, 1),
]  # fmt: skip
EPISODES = 6
MOVIE_SECTION, SHOW_SECTION = "1", "2"


def _colour(seed: str, shift: float = 0.0) -> tuple[int, int, int]:
    hue = (int(hashlib.sha256(seed.encode()).hexdigest()[:6], 16) / 0xFFFFFF + shift) % 1
    r, g, b = colorsys.hls_to_rgb(hue, 0.38, 0.65)
    return int(r * 255), int(g * 255), int(b * 255)


def art(path: str) -> Image.Image:
    """Soft gradient art with a few shapes, the same for the same path."""
    wide = "backdrop" in path or "still" in path
    size = (1920, 1080) if wide else (1000, 1500)
    top, bottom = _colour(path), _colour(path, 0.18)
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(size[1]):
        t = y / size[1]
        draw.line([(0, y), (size[0], y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    rng = random.Random(path)
    for _ in range(6):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.randrange(80, 420)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=_colour(path + str(r), 0.5))
    image = image.filter(ImageFilter.GaussianBlur(60))
    image.info["path"] = path
    return image


def no_text(_image: Image.Image) -> list[TextLine]:
    return []


def studio_poster(title: str) -> bytes:
    """The server's own poster: busy art with the name in it, as studios make them."""
    image = art(f"studio:{title}").resize((600, 900))
    ImageDraw.Draw(image).text((40, 780), title.upper(), fill=(255, 255, 255), font_size=44)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


class DemoTmdb(FakeTmdb):
    def __init__(self, names: dict[int, str]) -> None:
        super().__init__([ImageRef(f"/poster-{n}.jpg", None, 2000, 3000, 6 - n, 20 - n) for n in range(4)])
        self.names = names
        self.logos = []

    def details(self, kind: Kind, tmdb_id: int) -> Details:
        details = super().details(kind, tmdb_id)
        name = self.names.get(tmdb_id, "Example")
        return {**details, "title": name, "name": name}

    def images(self, kind: Kind, tmdb_id: int) -> Images:
        posters = [ImageRef(f"/{tmdb_id}-poster-{n}.jpg", None, 2000, 3000, 6 - n, 20 - n) for n in range(4)]
        backdrops = [ImageRef(f"/{tmdb_id}-backdrop-{n}.jpg", None, 3840, 2160, 5 - n, 9 - n) for n in range(3)]
        return Images(posters, backdrops, [])

    def season_images(self, show_id: int, season: int) -> Images:
        return Images([ImageRef(f"/{show_id}-season{season}.jpg", None, 2000, 3000, 5, 10)], [], [])

    def all_titles(self, kind: Kind, tmdb_id: int) -> list[str]:
        return [self.names.get(tmdb_id, "Example")]

    def watch_providers(self, kind: Kind, tmdb_id: int) -> dict[str, RegionOffers]:
        return {}

    def episode(self, show_id: int, season: int, episode: int) -> Episode | None:
        return {"still_path": f"/{show_id}-still-{season}-{episode}.jpg"}


class DemoServer(FakeServer):
    name = "Plex"
    url = "http://demo.plex:32400"

    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.kids: dict[str, list[str]] = {}
        self.chosen: dict[tuple[str, str], str] = {}
        self.uploaded: dict[str, bytes] = {}
        self.uploads = 0
        now = int(time.time())
        key = 100
        for number, (title, year) in enumerate(MOVIES):
            key += 1
            self.items[str(key)] = self._item(key, "movie", title, year, MOVIE_SECTION, "Movies", now - number * 86400)
        for number, (title, year, seasons) in enumerate(SHOWS):
            key += 1
            show = str(key)
            self.items[show] = self._item(key, "show", title, year, SHOW_SECTION, "TV Shows", now - number * 2 * 86400)
            self.kids[show] = []
            for season in range(1, seasons + 1):
                key += 1
                season_key = str(key)
                self.kids[show].append(season_key)
                self.items[season_key] = Item(
                    ratingKey=season_key, type="season", title=f"Season {season}", index=season,
                    parentRatingKey=show, librarySectionID=SHOW_SECTION, librarySectionTitle="TV Shows",
                    addedAt=now, updatedAt=now, Label=[], thumb=f"/library/metadata/{season_key}/thumb",
                )  # fmt: skip
                self.kids[season_key] = []
                for episode in range(1, EPISODES + 1):
                    key += 1
                    self.kids[season_key].append(str(key))
                    self.items[str(key)] = Item(
                        ratingKey=str(key), type="episode", title=f"Chapter {episode}", index=episode,
                        parentIndex=season, parentRatingKey=season_key, grandparentRatingKey=show,
                        librarySectionID=SHOW_SECTION, librarySectionTitle="TV Shows", addedAt=now, updatedAt=now,
                        Label=[], Media=[], thumb=f"/library/metadata/{key}/thumb",
                    )  # fmt: skip

    @staticmethod
    def _item(  # noqa: PLR0913, PLR0917
        key: int, kind: str, title: str, year: int, section: str, library: str, added: int
    ) -> Item:
        return Item(
            ratingKey=str(key), type=kind, title=title, year=year, librarySectionID=section,
            librarySectionTitle=library, Guid=[{"id": f"tmdb://{key}"}], addedAt=added, updatedAt=added,
            Label=[], Media=[], thumb=f"/library/metadata/{key}/thumb",
        )  # fmt: skip

    def tmdb_names(self) -> dict[int, str]:
        return {int(k): str(i["title"]) for k, i in self.items.items() if i.get("type") in ("movie", "show")}

    def server_id(self) -> str:
        return "plex:demo"

    def sections(self) -> list[Item]:
        return [
            Item(key=MOVIE_SECTION, title="Movies", type="movie"),
            Item(key=SHOW_SECTION, title="TV Shows", type="show"),
        ]

    def item(self, rating_key: str) -> Item | None:
        found = self.items.get(rating_key)
        return Item(**found) if found else None

    def children(self, rating_key: str) -> list[Item]:
        return [Item(**self.items[k]) for k in self.kids.get(rating_key, [])]

    def section_items(self, section_key: str, kind: str, **filters: Any) -> Iterable[Item]:
        label = filters.get("label")
        for item in self.items.values():
            if item.get("librarySectionID") != section_key or item.get("type") != kind:
                continue
            if label and label not in {t.get("tag") for t in item.get("Label") or []}:
                continue
            yield Item(**item)

    def changed_since(self, section_key: str, kind: str, since: int) -> list[Item]:
        return [i for i in self.section_items(section_key, kind) if int(i.get("updatedAt") or 0) >= since]

    def selected(self, rating_key: str, target: Target) -> str | None:
        return self.chosen.get((rating_key, target))

    def upload(self, rating_key: str, target: Target, jpeg: bytes) -> str:
        self.uploads += 1
        self.chosen[(rating_key, target)] = f"upload://{rating_key}/{target}/{self.uploads}"
        self.uploaded[self.chosen[(rating_key, target)]] = jpeg
        return self.chosen[(rating_key, target)]

    def restore(self, item: Item, target: Target) -> None:
        self.chosen.pop((str(item["ratingKey"]), target), None)

    def poster_bytes(self, item: Item) -> bytes:
        if chosen := self.chosen.get((str(item["ratingKey"]), "poster")):
            return self.uploaded[chosen]
        return studio_poster(str(item.get("title", "")))

    def image_bytes(self, item: Item, target: Target) -> bytes:
        if chosen := self.chosen.get((str(item["ratingKey"]), target)):
            return self.uploaded[chosen]
        if target == "poster":
            return self.poster_bytes(item)
        buffer = io.BytesIO()
        art(f"server-{target}:{item.get('ratingKey')}").resize((960, 540)).save(buffer, "JPEG", quality=80)
        return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", type=Path, help="data folder (default: a new temporary folder)")
    parser.add_argument("--live", action="store_true", help="DRY_RUN=false: 'upload' to the made-up server")
    args = parser.parse_args()
    data = args.data or Path(tempfile.mkdtemp(prefix="posteryard-demo-"))
    logfmt.setup()
    # Set in the environment, so settings saved in the web UI validate against the same values.
    os.environ.update({
        "TMDB_API_KEY": "demo", "PLEX_URL": DemoServer.url, "PLEX_TOKEN": "demo", "WEBHOOK_SECRET": "demo",
        "UI_PASSWORD": "demo", "DATA_DIR": str(data), "LISTEN_PORT": str(args.port),
        "DRY_RUN": "false" if args.live else "true", "EPISODE_THUMBNAILS": "titled",
    })  # fmt: skip
    cfg = config.load(config.environment())
    server = DemoServer()
    store = Store(cfg.state_path)
    worker = Worker(cfg, server, store, Notifier(), DemoTmdb(server.tmdb_names()))
    worker.ctx.sources.read, worker.ctx.sources.fetch, worker.ctx.sources.marks = no_text, art, None
    print(f"Demo data in {data}. Open http://localhost:{args.port} and sign in with: demo", flush=True)
    code: int = Service(cfg, server, store, worker).run()
    return code


if __name__ == "__main__":
    sys.exit(main())
