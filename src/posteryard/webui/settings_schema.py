"""Every setting the web UI can show and change, in the groups of the Settings page."""

from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["text", "secret", "switch", "menu", "multi", "number", "time", "list"]
Option = tuple[str, str] | tuple[str, str, str]


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    type: Kind = "text"
    help: str = ""
    default: str = ""
    placeholder: str = ""
    options: tuple[Option, ...] = ()
    readonly: bool = False
    extra: dict[str, int] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "help": self.help, "type": self.type,
            "options": [list(option) for option in self.options], "default": self.default,
            "placeholder": self.placeholder, "readonly": self.readonly, **self.extra,
        }  # fmt: skip


GROUPS = (
    "Media server", "Art", "Badges", "Labels", "Episodes and collections", "Live TV", "Alerts", "Schedule", "Web UI",
    "Advanced",
)  # fmt: skip

# fmt: off
SETTINGS = (
    # Media server
    Setting(
        "PLEX_URL", "Plex address", "Media server", placeholder="http://192.168.1.10:32400",
        help="Your Plex server's address on the network. Not localhost. Leave it empty for Jellyfin or Emby.",
    ),
    Setting(
        "PLEX_TOKEN", "Plex token", "Media server", "secret", placeholder="your-plex-token",
        help="Your Plex token. Leave it empty for Jellyfin or Emby.",
    ),
    Setting(
        "JELLYFIN_URL", "Jellyfin address", "Media server", placeholder="http://192.168.1.10:8096",
        help="Your Jellyfin server's address. Use it instead of the Plex address and token.",
    ),
    Setting(
        "JELLYFIN_API_KEY", "Jellyfin API key", "Media server", "secret",
        help="From API Keys in the Jellyfin dashboard.",
    ),
    Setting(
        "EMBY_URL", "Emby address", "Media server", placeholder="http://192.168.1.10:8096",
        help="Your Emby server's address. Use it instead of the Plex address and token.",
    ),
    Setting("EMBY_API_KEY", "Emby API key", "Media server", "secret", help="From API Keys in the Emby dashboard."),
    Setting(
        "LIBRARIES", "Libraries to manage", "Media server", "list", default="Movies,TV Shows",
        placeholder="Movies,TV Shows",
        help="Names as your server shows them, separated by commas. Movie and TV libraries only.",
    ),
    Setting(
        "DRY_RUN", "Preview only", "Media server", "switch", default="true",
        help="On saves previews and changes nothing on your server. Off uploads the images and locks them.",
    ),
    Setting(
        "WEBHOOK_SECRET", "Webhook secret", "Media server", "secret", placeholder="any-long-random-text",
        help="Any long random text. It is part of the webhook address, and the web UI password without one below.",
    ),
    # Art
    Setting(
        "TMDB_API_KEY", "TMDB API key", "Art", "secret", placeholder="your-tmdb-api-key",
        help="A free key from TMDB, under Settings, API. The API Read Access Token works too.",
    ),
    Setting(
        "FANART_API_KEY", "fanart.tv as a second source", "Art", "secret", placeholder="your-fanart-api-key",
        help="Your fanart.tv API key, from your fanart.tv profile. Used when TMDB has nothing clean.",
    ),
    Setting(
        "APPLE_ART", "Fill gaps with Apple TV art", "Art", "switch", default="false",
        help="Use Apple TV's key art when TMDB has no textless poster.",
    ),
    Setting(
        "TEXT_CHECK", "Check art for printed titles", "Art", "switch", default="true",
        help="Off saves about 350 MB of memory on small machines, but a few posters may show the title twice.",
    ),
    Setting(
        "LOGO_LANGUAGES", "Title logo languages, in order", "Art", "list", default="en", placeholder="en",
        help="Two-letter codes separated by commas, such as fr,en.",
    ),
    Setting(
        "PREFER_WORDMARK", "Prefer wide title logos", "Art", "switch", default="true",
        help="Off allows tall or square logos.",
    ),
    # Badges
    Setting(
        "QUALITY_MIN_VIDEO", "Badges for video from", "Badges", "menu", default="2160",
        options=(("off", "Off"), ("720", "720p", "HD badge"), ("1080", "1080p", "HD badge"),
                 ("2160", "4K", "4K badge")),
    ),
    Setting(
        "QUALITY_MIN_HDR", "Badges for HDR from", "Badges", "menu", default="hdr10",
        options=(("off", "Off"), ("hdr10", "HDR10"), ("hdr10plus", "HDR10+"), ("dolbyvision", "Dolby Vision")),
    ),
    Setting(
        "QUALITY_MIN_AUDIO", "Badges for audio from", "Badges", "menu", default="atmos",
        help="DTS:X counts as Atmos.",
        options=(("off", "Off"), ("5.1", "5.1"), ("7.1", "7.1"), ("atmos", "Atmos", "or DTS:X")),
    ),
    Setting(
        "QUALITY_ACCESSIBILITY", "Accessibility badges on movies", "Badges", "multi",
        help="Read from the track flags, or from track names such as English (SDH).",
        options=(("sdh", "SDH", "subtitles"), ("cc", "CC", "captions"), ("ad", "AD", "audio description")),
    ),
    # Labels
    Setting(
        "STATUS_LABELS", "Just added and new season labels", "Labels", "switch", default="true",
        help="JUST ADDED, NEW EPISODE and NEW SEASON.",
    ),
    Setting(
        "MAINTAINERR_URL", "Count down to Maintainerr deletions", "Labels", placeholder="http://192.168.1.10:6246",
        help="Your Maintainerr address, for the LEAVING IN 5 DAYS countdown.",
    ),
    Setting(
        "STREAMING_REGIONS", "Find streaming services in", "Labels", "list", default="US", placeholder="US",
        help="Country codes in order, separated by commas. Outside the US, put yours first, such as GB,US.",
    ),
    # Episodes and collections
    Setting(
        "EPISODE_THUMBNAILS", "Episode thumbnails", "Episodes and collections", "menu", default="plain",
        options=(("plain", "Plain", "still and shade"), ("titled", "Titled", "EPISODE N and title"),
                 ("off", "Off", "server's own")),
    ),
    Setting(
        "COLLECTION_POSTERS", "Posters for collections", "Episodes and collections", "switch", default="false",
        help="Give every collection a poster.",
    ),
    Setting(
        "SERVICE_COLLECTIONS", "A collection per streaming service", "Episodes and collections", "switch",
        default="false", help="Keep one collection per streaming service with at least 3 shows.",
    ),
    # Live TV
    Setting(
        "LIVE_TV", "Posters for the Live TV guide", "Live TV", "switch", default="false",
        help="Posters for the shows and movies in the Live TV guide, on Plex and Jellyfin.",
    ),
    # Alerts
    Setting(
        "NOTIFY_URLS", "Send alerts to", "Alerts", "secret", placeholder="discord://ID/TOKEN",
        help="Apprise addresses separated by spaces or commas, for Discord, Telegram, ntfy, email and about 100 more.",
    ),
    Setting(
        "NOTIFY_EVENTS", "Send alerts for", "Alerts", "multi", default="problems", extra={"min": 1},
        options=(("problems", "Problems"), ("new", "New posters"), ("summary", "Daily summary")),
    ),
    Setting(
        "HEARTBEAT_URL", "Call while healthy", "Alerts", "secret", placeholder="https://uptime.example/push",
        help="An address Posteryard calls every minute while healthy, such as an Uptime Kuma push URL.",
    ),
    # Schedule
    Setting(
        "SWEEP_MINUTES", "Check for new titles every", "Schedule", "number", default="15",
        extra={"min": 1, "max": 1440}, help="Minutes between checks for new and changed titles.",
    ),
    Setting(
        "DAILY_AT", "Full pass every day at", "Schedule", "time", default="04:15",
        help="24-hour time in the container's time zone, TZ.",
    ),
    # Web UI
    Setting(
        "UI_PASSWORD", "Web UI password", "Web UI", "secret",
        help="The password for this web UI. Without one, the webhook secret is the password.",
    ),
    Setting(
        "WEB_UI", "Web UI", "Web UI", "switch", default="true", readonly=True,
        help="Set WEB_UI=false in the compose file to turn the web UI off.",
    ),
    # Advanced
    Setting(
        "ONLY_RATING_KEYS", "Only these titles", "Advanced", "list", placeholder="5646,8730",
        help="Limit Posteryard to a few titles, for a first test. Rating keys separated by commas.",
    ),
    Setting(
        "LOG_LEVEL", "Log detail", "Advanced", "menu", default="info",
        options=(("debug", "Debug"), ("info", "Info"), ("warning", "Warning"), ("error", "Error")),
    ),
    Setting(
        "LISTEN_PORT", "Port inside the container", "Advanced", "number", default="8000", readonly=True,
        extra={"min": 1, "max": 65535}, help="Set in the compose file, with the right side of ports:.",
    ),
    Setting(
        "DATA_DIR", "Data folder inside the container", "Advanced", default="/data", readonly=True,
        help="Set in the compose file. Keep the default.",
    ),
)
# fmt: on

BY_KEY = {setting.key: setting for setting in SETTINGS}
