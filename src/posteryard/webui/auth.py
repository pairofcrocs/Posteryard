"""Sign-in for the web UI: one password, a signed cookie, and a pause after wrong passwords."""

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "posteryard_session"
SESSION_SECONDS = 30 * 24 * 3600
KEY_FILE = "ui-session.key"
FAILURE_WINDOW = 300
MAX_FAILURES = 10


class Auth:
    """The cookie is `<expiry>.<hmac>`. Its key mixes a random file in the data folder with the password, so a new
    password signs everyone out."""

    def __init__(self, data_dir: Path, password: str) -> None:
        self.password = password
        self._key = hashlib.sha256(_secret(data_dir) + password.encode()).digest()
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def check_password(self, attempt: str) -> bool:
        return self.enabled and hmac.compare_digest(attempt.encode(), self.password.encode())

    def issue(self, now: float | None = None) -> str:
        expiry = int((time.time() if now is None else now) + SESSION_SECONDS)
        return f"{expiry}.{self._sign(str(expiry))}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not token or not self.enabled:
            return False
        expiry, _, signature = token.partition(".")
        if not (expiry.isascii() and expiry.isdigit()) or not hmac.compare_digest(signature, self._sign(expiry)):
            return False
        return int(expiry) > (time.time() if now is None else now)

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW]
            self._failures[client] = recent
            return len(recent) >= MAX_FAILURES

    def failed(self, client: str, now: float | None = None) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(time.time() if now is None else now)

    def cookie(self, token: str, *, secure: bool = False) -> str:
        flags = "; Secure" if secure else ""
        return f"{COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict{flags}"

    @staticmethod
    def clear_cookie() -> str:
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"

    def _sign(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()


def _secret(data_dir: Path) -> bytes:
    """A random key kept in the data folder, so sessions survive a restart. Without a writable folder, one per run."""
    path = data_dir / KEY_FILE
    try:
        if path.is_file():
            return path.read_bytes()
        data_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        return key
    except OSError:
        return secrets.token_bytes(32)
