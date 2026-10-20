import json
import logging
import re
import threading
import time
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

import apprise
from apprise.attachment.memory import AttachMemory

log = logging.getLogger(__name__)
QUIET_SECONDS = 6 * 3600
RETRY_SECONDS = 600
NEW_LISTED = 10
SEPARATOR = re.compile(r"[,\s]+")
ALERT_STATE = "alert_state"


class Event(StrEnum):
    PROBLEMS = "problems"
    NEW = "new"
    SUMMARY = "summary"


class State(Protocol):
    def meta(self, key: str, default: str = "") -> str: ...
    def set_meta(self, key: str, value: str) -> None: ...


def split_urls(text: str) -> list[str]:
    return [url for url in SEPARATOR.split(text.strip()) if url]


def invalid_urls(urls: Sequence[str]) -> list[int]:
    """The 1-based positions of the addresses Apprise cannot use. Addresses hold secrets, so they are not echoed."""
    apprise_log = logging.getLogger("apprise")
    level = apprise_log.level
    apprise_log.setLevel(logging.CRITICAL + 1)
    try:
        return [n for n, url in enumerate(urls, 1) if not apprise.Apprise().add(url)]
    finally:
        apprise_log.setLevel(level)


def _load(state: State | None) -> tuple[dict[str, float], set[str]]:
    if state is None:
        return {}, set()
    try:
        saved = json.loads(state.meta(ALERT_STATE, "{}"))
        return {str(k): float(v) for k, v in saved["muted"].items()}, {str(c) for c in saved["open"]}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}, set()


class Notifier:
    def __init__(
        self,
        apprise_urls: Sequence[str] = (),
        events: frozenset[Event] = frozenset({Event.PROBLEMS}),
        state: State | None = None,
    ) -> None:
        self._apprise = apprise.Apprise()
        for address in apprise_urls:
            self._apprise.add(address)
        self.events = events
        self._state = state
        self._lock = threading.Lock()
        self._muted, self._open = _load(state)
        # When each open alert went out. Not saved: after a restart it is read back from the mute.
        self._since = {cause: self._muted[cause] - QUIET_SECONDS for cause in self._open if cause in self._muted}

    @property
    def configured(self) -> bool:
        return len(self._apprise) > 0

    def alert(self, cause: str, message: str) -> None:
        """A problem. A delivered alert mutes its cause for QUIET_SECONDS, a failed one for RETRY_SECONDS.
        Resolving a cause does not end the mute, so a flapping cause cannot flood."""
        if Event.PROBLEMS not in self.events or not self.configured:
            return
        with self._lock:
            now = time.time()
            if now < self._muted.get(cause, 0.0):
                return
            self._muted[cause] = now + RETRY_SECONDS
        delivered = self.send(cause, message)
        with self._lock:
            if delivered:
                self._muted[cause] = now + QUIET_SECONDS
                if cause not in self._open:
                    self._since[cause] = now
                self._open.add(cause)
            self._save()

    def resolve(self, cause: str, message: str) -> None:
        """The end of a problem. Sent only when the alert for the cause was delivered."""
        with self._lock:
            if cause not in self._open:
                return
            self._open.discard(cause)
            self._since.pop(cause, None)
            self._save()
        log.info("problem resolved", extra={"cause": cause})
        self.send(cause, message, apprise.NotifyType.SUCCESS)

    def open_alerts(self) -> list[tuple[str, float | None]]:
        """The causes alerted and not resolved yet, with when the alert went out."""
        with self._lock:
            return [(cause, self._since.get(cause)) for cause in sorted(self._open)]

    def new_posters(self, names: Sequence[str], poster: bytes | None = None) -> None:
        if Event.NEW not in self.events or not names:
            return
        listed = list(names[:NEW_LISTED])
        if len(names) > NEW_LISTED:
            listed.append(f"and {len(names) - NEW_LISTED} more")
        subject = "new poster" if len(names) == 1 else f"{len(names)} new posters"
        attach = None
        if poster and len(names) == 1:
            attach = AttachMemory(poster, "poster.jpg", "image/jpeg")  # type: ignore[no-untyped-call]
        self.send(subject, "\n".join(listed), apprise.NotifyType.INFO, attach)

    def summary(self, message: str, *, failed: bool) -> None:
        if Event.SUMMARY in self.events:
            kind = apprise.NotifyType.WARNING if failed else apprise.NotifyType.SUCCESS
            self.send("daily check", message, kind)

    def send(
        self,
        subject: str,
        message: str,
        kind: apprise.NotifyType = apprise.NotifyType.WARNING,
        attach: AttachMemory | None = None,
    ) -> bool:
        if not self.configured:
            return False
        if self._apprise.notify(body=message, title=f"Posteryard: {subject}", notify_type=kind, attach=attach):
            return True
        log.warning("notification not delivered to every NOTIFY_URLS service", extra={"subject": subject})
        return False

    def _save(self) -> None:
        if self._state is not None:
            self._state.set_meta(ALERT_STATE, json.dumps({"muted": self._muted, "open": sorted(self._open)}))
