import hashlib
import json
import logging
import re
import urllib.parse
from collections.abc import Iterator, Mapping
from typing import Any

from posteryard import http
from posteryard.server import Guide, Item, Target

log = logging.getLogger(__name__)

ENDPOINTS: dict[Target, tuple[str, str, str]] = {
    "poster": ("posters", "poster", "thumb"),
    "thumb": ("posters", "poster", "thumb"),
    "art": ("arts", "art", "art"),
}
TYPE_IDS = {"movie": 1, "show": 2, "season": 3, "episode": 4, "collection": 18}
PAGE = 200
JSON = {"Accept": "application/json"}
GUIDE_PROVIDER = re.compile(r"^tv\.plex\.providers\.epg\.[a-z0-9]+:\d{1,9}$")
GUIDE_KEY = re.compile(r"^[A-Za-z0-9%._~-]{1,2000}$")
GUIDE_PAGE = 2000
GUIDE_IDS = 300
GUIDE_ID_BATCHES = 100


class Plex:
    name = "Plex"
    collections_per_library = True

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self._machine_id: str | None = None
        self._guide_batches: dict[str, list[set[str]]] = {}

    def _url(self, path: str, **params: Any) -> str:
        return f"{self.url}{path}{'?' + urllib.parse.urlencode(params) if params else ''}"

    def _send(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> bytes:
        """Send the token as a header and refuse redirects. A redirect must never carry the token to another host."""
        return http.request(
            method,
            self._url(path, **(params or {})),
            headers={"X-Plex-Token": self.token, **(headers or {})},
            data=data,
            timeout=timeout,
            redirects=False,
        )

    def _get(self, path: str, **params: Any) -> Mapping[str, Any]:
        container: Mapping[str, Any] = json.loads(self._send("GET", path, params, headers=JSON))["MediaContainer"]
        return container

    def sections(self) -> list[Item]:
        sections: list[Item] = self._get("/library/sections").get("Directory", [])
        return sections

    def item(self, rating_key: str) -> Item | None:
        if not is_rating_key(rating_key):
            return None
        try:
            meta = self._get(f"/library/metadata/{_key(rating_key)}", includeGuids=1).get("Metadata") or []
        except http.HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return meta[0] if meta else None

    def children(self, rating_key: str) -> list[Item]:
        path = f"/library/metadata/{_key(rating_key)}/children"
        items: list[Item] = self._get(path, includeGuids=1).get("Metadata", [])
        return items

    def section_items(self, section_key: str, kind: str, **filters: Any) -> Iterator[Item]:
        start = 0
        while True:
            page = self._get(
                f"/library/sections/{_key(section_key)}/all",
                type=TYPE_IDS[kind],
                includeGuids=1,
                **filters,
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE},
            )
            items = page.get("Metadata", [])
            yield from items
            start += len(items)
            if not items or start >= int(page.get("totalSize", start)):
                return

    def newest_added(self, section_key: str, kind: str, **filters: Any) -> int | None:
        page = self._get(
            f"/library/sections/{_key(section_key)}/all",
            type=TYPE_IDS[kind],
            sort="addedAt:desc",
            **filters,
            **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 1},
        )
        items = page.get("Metadata") or []
        return int(items[0]["addedAt"]) if items and items[0].get("addedAt") else None

    def server_id(self) -> str:
        return f"plex:{self.machine_id()}"

    def machine_id(self) -> str:
        if self._machine_id is None:
            self._machine_id = str(self._get("/identity")["machineIdentifier"])
        return self._machine_id

    def _uri(self, rating_keys: list[str]) -> str:
        keys = ",".join(_key(k) for k in rating_keys)
        return f"server://{self.machine_id()}/com.plexapp.plugins.library/library/metadata/{keys}"

    def collection_children(self, rating_key: str) -> list[Item]:
        items: list[Item] = self._get(f"/library/collections/{_key(rating_key)}/children", includeGuids=1).get(
            "Metadata", []
        )
        return items

    def create_collection(self, section_key: str, kind: str, title: str, rating_keys: list[str]) -> str:
        answer = self._send(
            "POST",
            "/library/collections",
            {
                "type": TYPE_IDS[kind],
                "title": title,
                "smart": 0,
                "sectionId": _key(section_key),
                "uri": self._uri(rating_keys),
            },
            headers=JSON,
        )
        created = json.loads(answer)["MediaContainer"]["Metadata"][0]
        return str(created["ratingKey"])

    def add_to_collection(self, rating_key: str, members: list[str]) -> None:
        self._send("PUT", f"/library/collections/{_key(rating_key)}/items", {"uri": self._uri(members)})

    def remove_from_collection(self, rating_key: str, member: str) -> None:
        self._send("DELETE", f"/library/collections/{_key(rating_key)}/items/{_key(member)}")

    def delete_collection(self, rating_key: str) -> None:
        self._send("DELETE", f"/library/collections/{_key(rating_key)}")

    def set_label(self, section_key: str, kind: str, rating_key: str, label: str) -> None:
        """Replace the item's labels with one label."""
        self._send(
            "PUT",
            f"/library/sections/{_key(section_key)}/all",
            {"type": TYPE_IDS[kind], "id": _key(rating_key), "label[0].tag.tag": label, "label.locked": 1},
        )

    def changed_since(self, section_key: str, kind: str, since: int) -> list[Item]:
        """Items added or updated after a Unix time. A replaced file only changes `updatedAt`."""
        added = list(self.section_items(section_key, kind, **{"addedAt>>": since}))
        updated = list(self.section_items(section_key, kind, **{"updatedAt>>": since}))
        return list({str(i["ratingKey"]): i for i in added + updated}.values())

    def images(self, rating_key: str, target: Target) -> list[Item]:
        listing, _, _ = ENDPOINTS[target]
        images: list[Item] = self._get(f"/library/metadata/{_key(rating_key)}/{listing}").get("Metadata", [])
        return images

    def selected(self, rating_key: str, target: Target) -> str | None:
        return next((str(i.get("ratingKey")) for i in self.images(rating_key, target) if i.get("selected")), None)

    def upload(self, rating_key: str, target: Target, jpeg: bytes) -> str:
        listing, select, _ = ENDPOINTS[target]
        before = {str(i.get("ratingKey")) for i in self.images(rating_key, target)}
        digest = hashlib.sha1(jpeg, usedforsecurity=False).hexdigest()
        self._send(
            "POST",
            f"/library/metadata/{_key(rating_key)}/{listing}",
            data=jpeg,
            headers={"Content-Type": "image/jpeg"},
            timeout=120,
        )
        after = [str(i.get("ratingKey")) for i in self.images(rating_key, target)]
        # Plex names an upload after the SHA-1 of its bytes. Uploading the same bytes again adds no entry.
        same = [k for k in after if k.startswith("upload://") and k.endswith(f"/{digest}")]
        image_key = next(iter(same or [k for k in after if k not in before]), None)
        if not image_key:
            raise http.RequestError(f"upload left no {target} to select", self._url(f"/library/metadata/{rating_key}"))
        self._send("PUT", f"/library/metadata/{_key(rating_key)}/{select}", {"url": image_key})
        return image_key

    def restore(self, item: Item, target: Target) -> None:
        rating_key = str(item["ratingKey"])
        _, select, field = ENDPOINTS[target]
        original = next(
            (
                str(i["ratingKey"])
                for i in self.images(rating_key, target)
                if not str(i.get("ratingKey", "")).startswith("upload://")
            ),
            None,
        )
        self._send(
            "PUT",
            f"/library/sections/{_key(str(item['librarySectionID']))}/all",
            {"type": TYPE_IDS[str(item["type"])], "id": rating_key, f"{field}.locked": 0},
        )
        if original:
            self._send("PUT", f"/library/metadata/{_key(rating_key)}/{select}", {"url": original})

    def poster_bytes(self, item: Item) -> bytes:
        thumb = str(item.get("thumb") or "")
        if not thumb.startswith("/library/"):
            raise http.RequestError("the item has no poster", self._url(f"/library/metadata/{item.get('ratingKey')}"))
        return self._send("GET", thumb, timeout=60)

    def image_bytes(self, item: Item, target: Target) -> bytes:
        """The image the server shows now: poster, background or episode thumbnail."""
        path = str(item.get("art") if target == "art" else item.get("thumb") or "")
        if not path.startswith("/library/"):
            raise http.HttpError(404, self._url(f"/library/metadata/{item.get('ratingKey')}"), f"no {target}")
        return self._send("GET", path, timeout=60)

    def remove_label(self, item: Item, label: str) -> None:
        self._send(
            "PUT",
            f"/library/sections/{_key(str(item['librarySectionID']))}/all",
            {"type": TYPE_IDS[str(item["type"])], "id": item["ratingKey"], "label[].tag.tag-": label},
        )

    def lock(self, item: Item, target: Target) -> None:
        _, _, field = ENDPOINTS[target]
        self._send(
            "PUT",
            f"/library/sections/{_key(str(item['librarySectionID']))}/all",
            {"type": TYPE_IDS[str(item["type"])], "id": item["ratingKey"], f"{field}.locked": 1},
        )

    def guides(self) -> list[Guide]:
        found = []
        for dvr in self._get("/livetv/dvrs").get("Dvr") or []:
            provider = str(dvr.get("epgIdentifier") or "")
            if GUIDE_PROVIDER.match(provider):
                found.append(Guide(provider, int(dvr.get("refreshedAt") or 0)))
        return found

    def guide_airings(self, provider: str, start: int, end: int) -> list[Item]:
        """Every airing that ends after `start` and begins before `end`, on all channels."""
        self._guide_batches.pop(provider, None)
        airings: list[Item] = []
        while True:
            page = self._get(
                f"/{_provider(provider)}/grid",
                type="1,4",
                **{"endsAt>": start, "beginsAt<": end},
                **{"X-Plex-Container-Start": len(airings), "X-Plex-Container-Size": GUIDE_PAGE},
            )
            items = page.get("Metadata") or []
            airings += items
            if not items or len(airings) >= int(page.get("totalSize", len(airings))):
                return airings

    def guide_item(self, provider: str, key: str) -> Item | None:
        try:
            meta = self._get(f"/{_provider(provider)}/metadata/{_guide_key(key)}").get("Metadata") or []
        except http.HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return meta[0] if meta else None

    def guide_target(self, provider: str, key: str, guid: str) -> str | None:
        """Plex matches a name key loosely: "News" can return "News at Ten". The numeric id is exact."""

        def reaches(target: str | None) -> bool:
            item = self.guide_item(provider, target) if target else None
            return item is not None and str(item.get("guid") or "") == guid

        if not guid:
            return None
        if reaches(key):
            return key
        found = self._guide_id(provider, guid)
        return found if reaches(found) else None

    def _guide_id(self, provider: str, guid: str) -> str | None:
        """Plex lists no numeric ids. It returns a list of ids in order and leaves out ids it does not have."""
        if provider not in self._guide_batches:
            self._guide_batches[provider] = self._guide_walk(provider)
        batch = next((n for n, guids in enumerate(self._guide_batches[provider]) if guid in guids), None)
        if batch is None:
            return None
        start = 1 + batch * GUIDE_IDS
        end = start + GUIDE_IDS
        while end - start > 1:
            middle = (start + end) // 2
            start, end = (start, middle) if guid in self._guide_guids(provider, start, middle) else (middle, end)
        return str(start)

    def _guide_walk(self, provider: str) -> list[set[str]]:
        batches: list[set[str]] = []
        while len(batches) < GUIDE_ID_BATCHES:
            start = 1 + len(batches) * GUIDE_IDS
            try:
                guids = self._guide_guids(provider, start, start + GUIDE_IDS)
            except http.RequestError as exc:
                log.warning(
                    "the guide ids could not all be read, some titles may get no poster",
                    extra={"ids": start - 1, "reason": str(exc)},
                )
                return batches
            if not guids:
                return batches
            batches.append(guids)
        log.warning(
            "the guide has more entries than are read, some titles may get no poster",
            extra={"ids": GUIDE_ID_BATCHES * GUIDE_IDS},
        )
        return batches

    def _guide_guids(self, provider: str, start: int, end: int) -> set[str]:
        ids = ",".join(str(n) for n in range(start, end))
        try:
            items = self._get(f"/{_provider(provider)}/metadata/{ids}").get("Metadata") or []
        except http.HttpError as exc:
            if exc.status != 404:
                raise
            items = []
        return {str(i.get("guid") or "") for i in items}

    def guide_images(self, provider: str, key: str) -> list[Item]:
        images: list[Item] = self._get(f"/{_provider(provider)}/metadata/{_guide_key(key)}/posters").get("Metadata", [])
        return images

    def guide_upload(self, provider: str, key: str, jpeg: bytes) -> str:
        """Plex selects and locks the upload. A guide refresh drops the selection but keeps the image."""
        path = f"/{_provider(provider)}/metadata/{_guide_key(key)}/posters"
        self._send("POST", path, data=jpeg, headers={"Content-Type": "image/jpeg"}, timeout=120)
        digest = hashlib.sha1(jpeg, usedforsecurity=False).hexdigest()
        image_key = f"upload://posters/{digest}"
        if image_key not in {str(i.get("ratingKey")) for i in self.guide_images(provider, key)}:
            raise http.RequestError("upload left no poster to select", self._url(path))
        self.guide_select(provider, key, image_key)
        return image_key

    def guide_select(self, provider: str, key: str, image_key: str) -> None:
        self._send("PUT", f"/{_provider(provider)}/metadata/{_guide_key(key)}/poster", {"url": image_key})


def _provider(provider: str) -> str:
    if not GUIDE_PROVIDER.match(provider):
        raise ValueError(f"not a Plex guide: {provider!r}")
    return provider


def _guide_key(key: str) -> str:
    if not GUIDE_KEY.match(key):
        raise ValueError(f"not a Plex guide item: {key!r}")
    return key


def is_rating_key(text: str) -> bool:
    return text.isascii() and text.isdigit()


def _key(rating_key: str) -> str:
    if not is_rating_key(rating_key):
        raise ValueError(f"not a Plex rating key: {rating_key!r}")
    return rating_key
