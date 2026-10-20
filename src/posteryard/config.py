import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from pathlib import Path

from posteryard import notify
from posteryard.quality import ACCESSIBILITY, AudioLevel, Badge, HdrLevel, QualityMinimums, VideoLevel

TRUE = frozenset({"1", "true", "yes", "on"})
FALSE = frozenset({"0", "false", "no", "off"})
CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SECRETS = (
    "TMDB_API_KEY", "FANART_API_KEY", "PLEX_TOKEN", "JELLYFIN_API_KEY", "EMBY_API_KEY", "WEBHOOK_SECRET",
    "NOTIFY_URLS", "HEARTBEAT_URL", "UI_PASSWORD",
)  # fmt: skip
REMOVED_NTFY = ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN", "NTFY_TOKEN_FILE")
# Read from the environment only: a wrong value saved in the web UI would lock the web UI out.
ENV_ONLY = ("DATA_DIR", "LISTEN_PORT", "WEB_UI")
SETTINGS_FILE = "settings.json"

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EpisodeMode(StrEnum):
    PLAIN = "plain"
    TITLED = "titled"
    OFF = "off"


@dataclass(frozen=True)
class Config:
    tmdb_api_key: str
    plex_url: str
    plex_token: str
    jellyfin_url: str
    jellyfin_api_key: str
    emby_url: str
    emby_api_key: str
    maintainerr_url: str
    regions: tuple[str, ...]
    quality: QualityMinimums
    status_labels: bool
    accessibility: frozenset[Badge]
    episodes: EpisodeMode
    logo_languages: tuple[str, ...]
    prefer_wordmark: bool
    collection_posters: bool
    service_collections: bool
    data_dir: Path
    libraries: tuple[str, ...]
    dry_run: bool
    only_rating_keys: frozenset[str]
    webhook_secret: str
    sweep_minutes: int
    daily_at: time
    notify_urls: tuple[str, ...]
    notify_events: frozenset[notify.Event]
    listen_port: int
    heartbeat_url: str
    log_level: LogLevel = LogLevel.INFO
    fanart_api_key: str = ""
    apple_art: bool = False
    text_check: bool = True
    live_tv: bool = False
    web_ui: bool = True
    ui_password: str = ""

    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.db"


def _choice[E: StrEnum](env: Mapping[str, str], name: str, default: str, levels: type[E]) -> E:
    raw = env.get(name, "").strip().lower() or default
    try:
        return levels(raw)
    except ValueError:
        allowed = ", ".join(level.value for level in levels)
        raise ConfigError(f"{name} must be one of: {allowed}") from None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in TRUE | FALSE:
        raise ConfigError(f"{name} must be true or false")
    return raw in TRUE


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip() or str(default)
    if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
        raise ConfigError(f"{name} must be a whole number from {low} to {high}")
    return int(raw)


def _list(env: Mapping[str, str], name: str, default: str = "") -> tuple[str, ...]:
    return tuple(part.strip() for part in env.get(name, default).split(",") if part.strip())


def _accessibility(env: Mapping[str, str]) -> frozenset[Badge]:
    allowed = {badge.value: badge for badge in ACCESSIBILITY}
    names = [name.lower() for name in _list(env, "QUALITY_ACCESSIBILITY")]
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise ConfigError(f"QUALITY_ACCESSIBILITY takes sdh, cc and ad, not {', '.join(unknown)}")
    return frozenset(allowed[name] for name in names)


def _notify_events(env: Mapping[str, str]) -> frozenset[notify.Event]:
    allowed = {event.value: event for event in notify.Event}
    names = [name.lower() for name in _list(env, "NOTIFY_EVENTS")] or [notify.Event.PROBLEMS.value]
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise ConfigError(f"NOTIFY_EVENTS takes problems, new and summary, not {', '.join(unknown)}")
    return frozenset(allowed[name] for name in names)


def _languages(env: Mapping[str, str]) -> tuple[str, ...]:
    languages = tuple(code.lower() for code in _list(env, "LOGO_LANGUAGES", "en"))
    bad = [code for code in languages if not (len(code) == 2 and code.isascii() and code.isalpha())]
    if bad or not languages:
        raise ConfigError("LOGO_LANGUAGES takes two-letter language codes such as fr,en")
    return languages


def _clock(env: Mapping[str, str], name: str, default: str) -> time:
    match = CLOCK.match(env.get(name, "").strip() or default)
    if not match:
        raise ConfigError(f"{name} must be a 24-hour time such as 04:15")
    return time(int(match.group(1)), int(match.group(2)))


def _with_secret_files(env: Mapping[str, str]) -> dict[str, str]:
    merged = dict(env)
    for name in SECRETS:
        path = env.get(f"{name}_FILE", "").strip()
        if not path:
            continue
        if env.get(name, "").strip():
            raise ConfigError(f"Set {name} or {name}_FILE, not both")
        try:
            merged[name] = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{name}_FILE could not be read: {type(exc).__name__}") from None
    return merged


def load(env: Mapping[str, str] = os.environ) -> Config:
    env = _with_secret_files(env)
    tmdb_api_key = env.get("TMDB_API_KEY", "").strip()
    if not tmdb_api_key:
        raise ConfigError("TMDB_API_KEY is required")
    if removed := [name for name in REMOVED_NTFY if env.get(name, "").strip()]:
        raise ConfigError(f"{', '.join(removed)} no longer exist. Set NOTIFY_URLS=ntfys://TOKEN@HOST/TOPIC instead")
    notify_urls = tuple(notify.split_urls(env.get("NOTIFY_URLS", "")))
    bad = notify.invalid_urls(notify_urls)
    if bad:
        raise ConfigError(f"NOTIFY_URLS: address {', '.join(map(str, bad))} is not one Apprise supports")
    heartbeat_url = env.get("HEARTBEAT_URL", "").strip()
    if heartbeat_url and not heartbeat_url.startswith(("http://", "https://")):
        raise ConfigError("HEARTBEAT_URL must start with http:// or https://")
    library_setting = "LIBRARIES" if env.get("LIBRARIES", "").strip() else "PLEX_LIBRARIES"
    libraries = _list(env, library_setting, "Movies,TV Shows")
    if not libraries:
        raise ConfigError(f"{library_setting} needs at least one library name")
    return Config(
        tmdb_api_key=tmdb_api_key,
        plex_url=env.get("PLEX_URL", "").strip().rstrip("/"),
        plex_token=env.get("PLEX_TOKEN", "").strip(),
        jellyfin_url=env.get("JELLYFIN_URL", "").strip().rstrip("/"),
        jellyfin_api_key=env.get("JELLYFIN_API_KEY", "").strip(),
        emby_url=env.get("EMBY_URL", "").strip().rstrip("/"),
        emby_api_key=env.get("EMBY_API_KEY", "").strip(),
        maintainerr_url=env.get("MAINTAINERR_URL", "").strip().rstrip("/"),
        regions=tuple(r.upper() for r in _list(env, "STREAMING_REGIONS", "US")),
        quality=QualityMinimums(
            video=_choice(env, "QUALITY_MIN_VIDEO", "2160", VideoLevel),
            hdr=_choice(env, "QUALITY_MIN_HDR", "hdr10", HdrLevel),
            audio=_choice(env, "QUALITY_MIN_AUDIO", "atmos", AudioLevel),
        ),
        status_labels=_bool(env, "STATUS_LABELS", default=True),
        accessibility=_accessibility(env),
        episodes=_choice(env, "EPISODE_THUMBNAILS", "plain", EpisodeMode),
        logo_languages=_languages(env),
        prefer_wordmark=_bool(env, "PREFER_WORDMARK", default=True),
        collection_posters=_bool(env, "COLLECTION_POSTERS", default=False),
        service_collections=_bool(env, "SERVICE_COLLECTIONS", default=False),
        data_dir=Path(env.get("DATA_DIR", "data")),
        libraries=libraries,
        dry_run=_bool(env, "DRY_RUN", default=True),
        only_rating_keys=frozenset(_list(env, "ONLY_RATING_KEYS")),
        webhook_secret=env.get("WEBHOOK_SECRET", "").strip(),
        sweep_minutes=_int(env, "SWEEP_MINUTES", 15, 1, 1440),
        daily_at=_clock(env, "DAILY_AT", "04:15"),
        notify_urls=notify_urls,
        notify_events=_notify_events(env),
        listen_port=_int(env, "LISTEN_PORT", 8000, 1, 65535),
        heartbeat_url=heartbeat_url,
        log_level=_choice(env, "LOG_LEVEL", "info", LogLevel),
        fanart_api_key=env.get("FANART_API_KEY", "").strip(),
        apple_art=_bool(env, "APPLE_ART", default=False),
        text_check=_bool(env, "TEXT_CHECK", default=True),
        live_tv=_bool(env, "LIVE_TV", default=False),
        web_ui=_bool(env, "WEB_UI", default=True),
        ui_password=env.get("UI_PASSWORD", "").strip(),
    )


def require_server(cfg: Config) -> None:
    servers = {
        ("PLEX_URL", "PLEX_TOKEN"): (cfg.plex_url, cfg.plex_token),
        ("JELLYFIN_URL", "JELLYFIN_API_KEY"): (cfg.jellyfin_url, cfg.jellyfin_api_key),
        ("EMBY_URL", "EMBY_API_KEY"): (cfg.emby_url, cfg.emby_api_key),
    }
    chosen = [names for names, values in servers.items() if any(values)]
    if len(chosen) > 1:
        raise ConfigError(f"Set one media server only, not {' and '.join(names[0] for names in chosen)}")
    if not chosen:
        raise ConfigError(
            "Set PLEX_URL and PLEX_TOKEN, or JELLYFIN_URL and JELLYFIN_API_KEY, or EMBY_URL and EMBY_API_KEY"
        )
    if not all(servers[chosen[0]]):
        raise ConfigError(f"{chosen[0][0]} and {chosen[0][1]} are both required")


def require_service(cfg: Config) -> None:
    require_server(cfg)
    if not cfg.webhook_secret:
        raise ConfigError("WEBHOOK_SECRET is required to run the service")


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    return Path(env.get("DATA_DIR", "data"))


def settings_path(folder: Path) -> Path:
    return folder / SETTINGS_FILE


def saved_settings(folder: Path) -> dict[str, str]:
    """The values saved in the web UI. A missing file is none; a broken one is logged and ignored."""
    path = settings_path(folder)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.warning(
            "settings from the web UI not read, using the environment",
            extra={"path": str(path), "reason": type(exc).__name__},
        )
        return {}
    if not isinstance(data, dict):
        log.warning(
            "settings from the web UI not read, using the environment",
            extra={"path": str(path), "reason": "not an object"},
        )
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str) and k not in ENV_ONLY}


def save_settings(folder: Path, values: Mapping[str, str]) -> Path:
    """Write the web UI's values at once: a crash leaves the old file or the new one, never half of one."""
    path = settings_path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=folder, prefix=".settings-", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(sorted(values.items())), handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return path


def environment(env: Mapping[str, str] = os.environ, folder: Path | None = None) -> dict[str, str]:
    """The environment with the web UI's saved values on top. DATA_DIR comes from the environment only."""
    return overlay(env, saved_settings(data_dir(env) if folder is None else folder))


def overlay(env: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    """`values` over `env`. A secret set here replaces its _FILE, which would otherwise clash with it."""
    merged = dict(env)
    for name, value in values.items():
        merged[name] = value
        if name in SECRETS:
            merged.pop(f"{name}_FILE", None)
    return merged


def secret(env: Mapping[str, str], name: str) -> str:
    """A secret from the environment or its _FILE, or "" when it cannot be read."""
    try:
        return _with_secret_files(env).get(name, "").strip()
    except ConfigError:
        return env.get(name, "").strip()


def listen_port(env: Mapping[str, str]) -> int:
    """LISTEN_PORT, or 8000 when it is missing or wrong."""
    try:
        return _int(env, "LISTEN_PORT", 8000, 1, 65535)
    except ConfigError:
        return 8000
