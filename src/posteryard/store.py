import contextlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from posteryard.overrides import Override

SCHEMA_1 = """
CREATE TABLE IF NOT EXISTS images (
    rating_key  TEXT NOT NULL,
    target      TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    image_key   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    failures    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (rating_key, target)
);
CREATE TABLE IF NOT EXISTS overrides (
    rating_key TEXT PRIMARY KEY,
    custom     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    skip       TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS choices (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
GUIDE_TABLE = """
CREATE TABLE IF NOT EXISTS guide (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    image_key   TEXT NOT NULL DEFAULT '',
    picture     TEXT NOT NULL DEFAULT '',
    seen_at     INTEGER NOT NULL DEFAULT 0
);
"""
# Append only. Entry N brings the database from version N to N + 1. Never edit a released entry.
MIGRATIONS = (SCHEMA_1, GUIDE_TABLE)
COLUMNS = "rating_key, target, title, fingerprint, image_key, status, failures, updated_at"
RETRY_FIRST = 900
RETRY_MAX = 12 * 3600


class StoreError(Exception):
    pass


class Status(StrEnum):
    UPLOADED = "uploaded"
    UPLOADING = "uploading"
    PREVIEW = "preview"
    MANUAL = "manual"
    FAILED = "failed"


@dataclass(frozen=True)
class GuideRecord:
    key: str
    title: str
    fingerprint: str
    image_key: str
    picture: str
    seen_at: int


@dataclass(frozen=True)
class Record:
    rating_key: str
    target: str
    title: str
    fingerprint: str
    image_key: str
    status: str
    failures: int
    updated_at: int


class Store:
    def __init__(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
        files = (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm"))
        blocked = [path.parent] if not os.access(path.parent, os.W_OK | os.X_OK) else []
        blocked += [f for f in files if f.exists() and not os.access(f, os.W_OK)]
        if blocked:
            raise StoreError(
                f"{blocked[0]} is not writable by user {os.getuid()}:{os.getgid()}. "
                "Give this user ownership of the folder mounted there and everything in it."
            )
        try:
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:
            raise StoreError(f"{path} could not be opened: {exc}") from None
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > len(MIGRATIONS):
            self._db.close()
            raise StoreError(
                f"state.db has schema version {version}, newer than this Posteryard supports. Run a newer release."
            )
        for number, script in enumerate(MIGRATIONS[version:], version + 1):
            self._db.executescript(f"BEGIN; {script} PRAGMA user_version={number}; COMMIT;")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self, rating_key: str, target: str) -> Record | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {COLUMNS} FROM images WHERE rating_key=? AND target=?",
                (rating_key, target),
            ).fetchone()
        return Record(*row) if row else None

    def uploaded(self, rating_key: str, target: str, title: str, fingerprint: str, image_key: str) -> None:
        self._upsert(rating_key, target, title=title, fingerprint=fingerprint, image_key=image_key,
                     status=Status.UPLOADED, failures=0, last_error="")  # fmt: skip

    def uploading(self, rating_key: str, target: str, title: str) -> None:
        self._upsert(rating_key, target, title=title, status=Status.UPLOADING)

    def previewed(self, rating_key: str, target: str, title: str, fingerprint: str) -> None:
        self._upsert(rating_key, target, title=title, fingerprint=fingerprint, status=Status.PREVIEW,
                     failures=0, last_error="")  # fmt: skip

    def manual(self, rating_key: str, target: str, title: str) -> None:
        self._upsert(rating_key, target, title=title, status=Status.MANUAL)

    def failed(self, rating_key: str, target: str, title: str, error: str) -> int:
        record = self.get(rating_key, target)
        failures = (record.failures if record else 0) + 1
        self._upsert(rating_key, target, title=title, status=Status.FAILED, failures=failures, last_error=error[:500])
        return failures

    def forget_target(self, rating_key: str, target: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM images WHERE rating_key=? AND target=?", (rating_key, target))

    def forget(self, rating_key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM images WHERE rating_key=?", (rating_key,))

    def retry_due(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT rating_key, failures, updated_at FROM images WHERE status=?", (Status.FAILED,)
            ).fetchall()
        due = {key for key, fails, at in rows if now - at >= min(RETRY_FIRST * 2 ** (fails - 1), RETRY_MAX)}
        return sorted(due)

    def with_upload(self) -> list[Record]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {COLUMNS} FROM images WHERE image_key != '' OR status=? ORDER BY rating_key, target",
                (Status.UPLOADING,),
            ).fetchall()
        return [Record(*row) for row in rows]

    def keys(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self._db.execute("SELECT DISTINCT rating_key FROM images")}

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute("SELECT status, COUNT(*) FROM images GROUP BY status").fetchall())

    def records(
        self, *, status: str | None = None, rating_key: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[Record]:
        """Image records, most recently changed first, for the web UI."""
        where, values = [], []
        if status is not None:
            where.append("status=?")
            values.append(status)
        if rating_key is not None:
            where.append("rating_key=?")
            values.append(rating_key)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {COLUMNS} FROM images {clause} ORDER BY updated_at DESC, rating_key, target LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [Record(*row) for row in rows]

    def count(self, status: str | None = None) -> int:
        with self._lock:
            if status is None:
                return int(self._db.execute("SELECT COUNT(*) FROM images").fetchone()[0])
            return int(self._db.execute("SELECT COUNT(*) FROM images WHERE status=?", (status,)).fetchone()[0])

    def last_error(self, rating_key: str, target: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT last_error FROM images WHERE rating_key=? AND target=?", (rating_key, target)
            ).fetchone()
        return str(row[0]) if row else ""

    def statuses(self, target: str = "poster") -> dict[str, str]:
        """Each rating key's status for one target, to mark many titles at once."""
        with self._lock:
            rows = self._db.execute("SELECT rating_key, status FROM images WHERE target=?", (target,)).fetchall()
        return {str(key): str(status) for key, status in rows}

    def all_overrides(self) -> dict[str, Override]:
        with self._lock:
            rows = self._db.execute("SELECT rating_key, custom, source, skip FROM overrides").fetchall()
        return {
            str(key): Override(custom=custom or None, source=source, skip=frozenset(json.loads(skip)))
            for key, custom, source, skip in rows
        }

    def guides(self) -> list[GuideRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, title, fingerprint, image_key, picture, seen_at FROM guide ORDER BY title, key"
            ).fetchall()
        return [GuideRecord(*row) for row in rows]

    def override(self, rating_key: str) -> Override | None:
        with self._lock:
            row = self._db.execute(
                "SELECT custom, source, skip FROM overrides WHERE rating_key=?", (rating_key,)
            ).fetchone()
        if not row:
            return None
        return Override(custom=row[0] or None, source=row[1], skip=frozenset(json.loads(row[2])))

    def set_custom(self, rating_key: str, path: str, source: str) -> None:
        self._release_custom(rating_key, keep=path)
        with self._lock:
            self._db.execute(
                "INSERT INTO overrides(rating_key, custom, source, skip) VALUES(?, ?, ?, '[]') "
                "ON CONFLICT(rating_key) DO UPDATE SET custom=excluded.custom, source=excluded.source, skip='[]'",
                (rating_key, path, source),
            )

    def add_skip(self, rating_key: str, art: str) -> None:
        self._release_custom(rating_key)
        current = self.override(rating_key)
        skip = sorted((current.skip if current else frozenset()) | {art})
        with self._lock:
            self._db.execute(
                "INSERT INTO overrides(rating_key, custom, source, skip) VALUES(?, '', '', ?) "
                "ON CONFLICT(rating_key) DO UPDATE SET custom='', source='', skip=excluded.skip",
                (rating_key, json.dumps(skip)),
            )

    def reset_override(self, rating_key: str) -> None:
        self._release_custom(rating_key)
        with self._lock:
            self._db.execute("DELETE FROM overrides WHERE rating_key=?", (rating_key,))

    def _release_custom(self, rating_key: str, keep: str = "") -> None:
        """Delete the custom art file the override no longer uses. Only files in a `custom` folder are touched."""
        current = self.override(rating_key)
        if current and current.custom and current.custom != keep and Path(current.custom).parent.name == "custom":
            Path(current.custom).unlink(missing_ok=True)

    def get_choice(self, key: str) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM choices WHERE key=?", (key,)).fetchone()
        value: Mapping[str, Any] | None = json.loads(row[0]) if row else None
        return value

    def put_choice(self, key: str, value: Mapping[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO choices(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def drop_choices(self, family: str, keep: tuple[str, ...]) -> int:
        """Delete the choices whose key starts with `family` but with none of `keep`."""
        kept = " ".join("AND substr(key, 1, ?) != ?" for _ in keep)
        values = [value for prefix in keep for value in (len(prefix), prefix)]
        with self._lock:
            cursor = self._db.execute(
                f"DELETE FROM choices WHERE substr(key, 1, ?) = ? {kept}", (len(family), family, *values)
            )
        return cursor.rowcount

    def guide(self, key: str) -> GuideRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT key, title, fingerprint, image_key, picture, seen_at FROM guide WHERE key=?", (key,)
            ).fetchone()
        return GuideRecord(*row) if row else None

    def guide_uploaded(self, key: str, title: str, fingerprint: str, image_key: str, picture: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO guide(key, title, fingerprint, image_key, picture, seen_at) VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET title=excluded.title, fingerprint=excluded.fingerprint, "
                "image_key=excluded.image_key, picture=excluded.picture, seen_at=excluded.seen_at",
                (key, title, fingerprint, image_key, picture, int(time.time())),
            )

    def guide_seen(self, keys: list[str], now: float | None = None) -> None:
        stamp = int(time.time() if now is None else now)
        with self._lock:
            self._db.executemany("UPDATE guide SET seen_at=? WHERE key=?", [(stamp, key) for key in keys])

    def guide_drop_unseen(self, before: float) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM guide WHERE seen_at < ?", (int(before),)).rowcount

    def meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _upsert(self, rating_key: str, target: str, **fields: object) -> None:
        fields["updated_at"] = int(time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{c}=excluded.{c}" for c in fields)
        with self._lock:
            self._db.execute(
                f"INSERT INTO images(rating_key, target, {cols}) VALUES(?, ?, {marks}) "
                f"ON CONFLICT(rating_key, target) DO UPDATE SET {updates}",
                (rating_key, target, *fields.values()),
            )
