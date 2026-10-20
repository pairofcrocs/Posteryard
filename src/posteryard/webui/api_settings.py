"""Web UI API: settings. See docs/webui-api.md."""

import logging
import os
import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from posteryard import config, http, notify
from posteryard.fanart import Fanart
from posteryard.maintainerr import Maintainerr
from posteryard.tmdb import Tmdb
from posteryard.webui.auth import Auth
from posteryard.webui.routing import ApiError, Request, Response, json_response
from posteryard.webui.settings_schema import BY_KEY, GROUPS, SETTINGS, Setting

if TYPE_CHECKING:
    from posteryard.webui.app import WebUi

log = logging.getLogger(__name__)
TEST_SECONDS = 20.0
OLD_NAMES = {"LIBRARIES": "PLEX_LIBRARIES"}
Check = Callable[[dict[str, str]], tuple[bool, str]]


def register(ui: "WebUi") -> None:
    """Add this module's routes to ui.router. In setup mode, the restart action too."""
    r = ui.router
    lock = threading.Lock()

    def first_run() -> bool:
        """Setup mode with no password anywhere: the first visitor chooses it."""
        return ui.setup and not ui.auth.enabled

    def require(req: Request) -> None:
        if not (ui.authenticated(req) or first_run()):
            raise ApiError(401, "sign in first")

    @r.get("/api/settings", public=True)
    def read(req: Request) -> Response:
        require(req)
        return json_response(payload(ui))

    @r.put("/api/settings", public=True)
    def save(req: Request) -> Response:
        require(req)
        with lock:
            return _save(ui, req)

    @r.post("/api/settings/test", public=True)
    def test(req: Request) -> Response:
        require(req)
        return _test(ui, req.json())

    if ui.setup:

        @r.post("/api/actions/restart")
        def restart(_req: Request) -> Response:
            if ui.on_restart is None:
                raise ApiError(409, "Restart the container to apply the settings")
            ui.on_restart()
            return json_response({"ok": True})


def _test(ui: "WebUi", body: dict[str, Any]) -> Response:
    check = CHECKS.get(str(body.get("what", "")))
    if check is None:
        raise ApiError(400, f"what must be one of: {', '.join(CHECKS)}")
    env = os.environ
    merged = config.overlay(env, _apply(config.saved_settings(ui.data_dir), _values(body.get("values")), env))
    ok, message = within(TEST_SECONDS, check, merged)
    return json_response({"ok": ok, "message": message})


def _save(ui: "WebUi", req: Request) -> Response:
    signed_in = ui.authenticated(req)
    values = _values(req.json().get("values"))
    if not signed_in and not values.get("UI_PASSWORD"):
        raise ApiError(400, "Choose a password for the web UI first")
    env = os.environ
    updated = _apply(config.saved_settings(ui.data_dir), values, env)
    merged = config.overlay(env, updated)
    # Setup mode saves what it is given, so the settings can be fixed one at a time; GET's error says what is left.
    problem = None if ui.setup else _problem(merged)
    if problem:
        raise ApiError(400, problem)
    try:
        path = config.save_settings(ui.data_dir, updated)
    except OSError as exc:
        raise ApiError(500, f"Could not save {config.settings_path(ui.data_dir)}: {exc.strerror}") from None
    ui.restart_required = True
    log.info("settings saved in the web UI", extra={"path": str(path), "changed": ", ".join(sorted(values))})
    headers = {}
    password = config.secret(merged, "UI_PASSWORD") or config.secret(merged, "WEBHOOK_SECRET")
    if password != ui.auth.password:
        # A new password applies at once and signs everyone else out.
        ui.auth = Auth(ui.data_dir, password)
        if password:
            secure = req.headers.get("X-Forwarded-Proto", "").lower() == "https"
            headers["Set-Cookie"] = ui.auth.cookie(ui.auth.issue(), secure=secure)
    return json_response(payload(ui), headers=headers)


def payload(ui: "WebUi") -> dict[str, Any]:
    env = os.environ
    saved = config.saved_settings(ui.data_dir)
    groups = [
        {"name": name, "fields": [_field(s, env, saved, ui) for s in SETTINGS if s.group == name]} for name in GROUPS
    ]
    return {
        "groups": groups,
        "restart_required": ui.restart_required,
        "error": _problem(config.overlay(env, saved)) if ui.setup else None,
        "path": str(config.settings_path(ui.data_dir)),
    }


def _field(setting: Setting, env: Mapping[str, str], saved: Mapping[str, str], ui: "WebUi") -> dict[str, Any]:
    from_env = _from_env(env, setting.key)
    if setting.key == "DATA_DIR":
        source, value = ("env" if from_env else "default"), str(ui.data_dir)
    elif setting.key in saved:
        source, value = "ui", saved[setting.key]
    elif from_env is not None:
        source, value = "env", from_env
    else:
        source, value = "default", setting.default
    field = setting.public() | {"value": value, "source": source}
    if setting.type == "secret":
        field |= {"value": "", "is_set": bool(value) or source == "env"}
    return field


def _from_env(env: Mapping[str, str], key: str) -> str | None:
    """The environment's own value, or None when it leaves the setting to its default."""
    for name in (key, OLD_NAMES.get(key)):
        if name and env.get(name, "").strip():
            return env[name].strip()
    if key in config.SECRETS and env.get(f"{key}_FILE", "").strip():
        return ""
    return None


def _values(raw: object) -> dict[str, str | None]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ApiError(400, "values must be an object")
    values: dict[str, str | None] = {}
    for key, value in raw.items():
        setting = BY_KEY.get(key)
        if setting is None:
            raise ApiError(400, f"{key} is not a setting")
        if setting.readonly:
            raise ApiError(400, f"{key} can only be set in the compose file")
        if value is not None and not isinstance(value, str):
            raise ApiError(400, f"{key} must be text or null")
        values[key] = value.strip() if isinstance(value, str) else None
    return values


def _apply(saved: Mapping[str, str], values: Mapping[str, str | None], env: Mapping[str, str]) -> dict[str, str]:
    """The saved values after a change. null drops a value; "" keeps a secret; a value the same as the
    environment's or the default is not saved, so a later change in the compose file still applies."""
    updated = dict(saved)
    for key, value in values.items():
        setting = BY_KEY[key]
        if value is None:
            updated.pop(key, None)
        elif setting.type == "secret" and not value:
            continue
        elif key in updated or setting.type == "secret" or value != _effective(env, setting):
            updated[key] = value
    return updated


def _effective(env: Mapping[str, str], setting: Setting) -> str:
    from_env = _from_env(env, setting.key)
    return setting.default if from_env is None else from_env


def _problem(env: Mapping[str, str]) -> str | None:
    try:
        config.require_service(config.load(env))
    except config.ConfigError as exc:
        return str(exc)
    return None


def within(seconds: float, check: Check, env: dict[str, str]) -> tuple[bool, str]:
    """Run a connection check, giving up after `seconds`. A failure is an answer, never an exception."""
    result: list[tuple[bool, str]] = []

    def run() -> None:
        try:
            result.append(check(env))
        except (http.RequestError, config.ConfigError, ValueError) as exc:
            result.append((False, str(exc)))
        except Exception as exc:
            log.warning("settings check failed", extra={"reason": f"{type(exc).__name__}: {exc}"})
            result.append((False, f"{type(exc).__name__}: {exc}"))

    thread = threading.Thread(target=run, daemon=True, name="settings-check")
    thread.start()
    thread.join(seconds)
    return result[0] if result else (False, f"No answer within {seconds:.0f} seconds")


def _cfg(env: Mapping[str, str]) -> config.Config:
    """The settings, without needing the TMDB key, for checks that do not use it."""
    if config.secret(env, "TMDB_API_KEY"):
        return config.load(env)
    lenient = {k: v for k, v in env.items() if k != "TMDB_API_KEY_FILE"}
    return config.load(lenient | {"TMDB_API_KEY": "unset"})


def _server(env: dict[str, str]) -> tuple[bool, str]:
    from posteryard.cli import media_server  # noqa: PLC0415 - cli imports the web UI

    cfg = _cfg(env)
    config.require_server(cfg)
    server = media_server(cfg)
    titles = [str(section.get("title", "")) for section in server.sections()]
    missing = [name for name in cfg.libraries if name not in titles]
    message = f"{server.name} answered. Its libraries: {', '.join(titles) or 'none'}."
    if missing:
        message += f" Not found: {', '.join(missing)}."
    return len(missing) < len(cfg.libraries), message


def _tmdb(env: dict[str, str]) -> tuple[bool, str]:
    key = config.secret(env, "TMDB_API_KEY")
    if not key:
        return False, "Set the TMDB API key first"
    details = Tmdb(key).details("movie", 603)
    return True, f"TMDB answered with {details.get('title', 'a movie')}."


def _fanart(env: dict[str, str]) -> tuple[bool, str]:
    key = config.secret(env, "FANART_API_KEY")
    if not key:
        return False, "Set the fanart.tv API key first"
    Fanart(key).images("movie", 603, None)
    return True, "fanart.tv answered."


def _maintainerr(env: dict[str, str]) -> tuple[bool, str]:
    url = _cfg(env).maintainerr_url
    if not url:
        return False, "Set the Maintainerr address first"
    days = Maintainerr(url).action_days()
    return True, f"Maintainerr answered. {len(days)} titles have a deletion date."


def _notify(env: dict[str, str]) -> tuple[bool, str]:
    urls = notify.split_urls(config.secret(env, "NOTIFY_URLS"))
    if not urls:
        return False, "Set an address to send alerts to first"
    if bad := notify.invalid_urls(urls):
        return False, f"Address {', '.join(map(str, bad))} is not one Apprise supports"
    if notify.Notifier(urls).send("test", "Alerts from Posteryard reach you."):
        return True, "Test alert sent."
    return False, "A notification service did not accept the test alert. The log has the details."


CHECKS: dict[str, Check] = {
    "server": _server,
    "tmdb": _tmdb,
    "fanart": _fanart,
    "maintainerr": _maintainerr,
    "notify": _notify,
}
