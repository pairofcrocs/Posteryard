import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from posteryard import cli, config, http, notify, quality
from posteryard.config import EpisodeMode, LogLevel
from posteryard.quality import AudioLevel, HdrLevel, VideoLevel
from posteryard.server import Item
from posteryard.service import Service
from posteryard.store import Store
from posteryard.webui import api_settings
from posteryard.webui.app import WebUi
from posteryard.webui.auth import COOKIE
from posteryard.webui.routing import Request, Response
from posteryard.webui.settings_schema import BY_KEY, GROUPS, SETTINGS
from tests import fakes
from tests.fakes import FakeServer

ROOT = Path(__file__).parent.parent
ENV = {
    "TMDB_API_KEY": "env-tmdb", "PLEX_URL": "http://plex.example:32400", "PLEX_TOKEN": "env-token",
    "WEBHOOK_SECRET": "env-secret",
}  # fmt: skip
CHOICES: dict[str, set[str]] = {
    "QUALITY_MIN_VIDEO": {level.value for level in VideoLevel},
    "QUALITY_MIN_HDR": {level.value for level in HdrLevel},
    "QUALITY_MIN_AUDIO": {level.value for level in AudioLevel},
    "QUALITY_ACCESSIBILITY": {badge.value for badge in quality.ACCESSIBILITY},
    "EPISODE_THUMBNAILS": {mode.value for mode in EpisodeMode},
    "NOTIFY_EVENTS": {event.value for event in notify.Event},
    "LOG_LEVEL": {level.value for level in LogLevel},
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The environment of a working install; returns the data folder."""
    for name in [*config.SECRETS, *(f"{n}_FILE" for n in config.SECRETS), "LIBRARIES", "PLEX_LIBRARIES"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in {**ENV, "DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def service_ui(data: Path) -> WebUi:
    cfg = config.load(config.environment())
    server = FakeServer()
    store = Store(cfg.state_path)
    service = Service(cfg, server, store, fakes.worker(cfg, server, store))
    service.attach_ui()
    assert service.ui is not None
    return service.ui


def call(ui: WebUi, method: str, path: str, body: Any = None, *, signed_in: bool = True) -> Response:
    headers = {"X-Requested-With": "posteryard"}
    if signed_in:
        headers["Cookie"] = f"{COOKIE}={ui.auth.issue()}"
    data = json.dumps(body).encode() if body is not None else b""
    return ui.dispatch(Request(method, path, {}, headers, data, client="127.0.0.1"))


def answer(response: Response) -> Any:
    return json.loads(response.body)


def fields(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["key"]: f for group in data["groups"] for f in group["fields"]}


# Overlay


def test_saved_values_win_over_the_environment(tmp_path: Path) -> None:
    config.save_settings(tmp_path, {"LOG_LEVEL": "debug", "DATA_DIR": "/elsewhere", "WEB_UI": "false"})
    merged = config.environment({"DATA_DIR": str(tmp_path), "LOG_LEVEL": "warning", "WEB_UI": "true"})
    assert merged["LOG_LEVEL"] == "debug"
    assert (merged["DATA_DIR"], merged["WEB_UI"]) == (str(tmp_path), "true")
    assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_a_saved_secret_replaces_its_file(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("from-file")
    config.save_settings(tmp_path, {"PLEX_TOKEN": "from-ui"})
    env = {"DATA_DIR": str(tmp_path), "TMDB_API_KEY": "x", "PLEX_TOKEN_FILE": str(tmp_path / "token")}
    assert config.load(config.environment(env)).plex_token == "from-ui"
    assert config.load(env).plex_token == "from-file"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", b"\xff\xfe"])
def test_a_broken_settings_file_is_ignored(
    tmp_path: Path, content: str | bytes, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    assert config.environment({"DATA_DIR": str(tmp_path), "X": "1"}) == {"DATA_DIR": str(tmp_path), "X": "1"}
    assert "settings from the web UI not read" in caplog.text


def test_a_missing_settings_file_is_no_values(tmp_path: Path) -> None:
    assert config.saved_settings(tmp_path / "nowhere") == {}


def test_a_failed_save_leaves_no_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: Path, target: Path) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="No space"):
        config.save_settings(tmp_path, {"LOG_LEVEL": "debug"})
    assert list(tmp_path.iterdir()) == []


def test_lenient_readers() -> None:
    assert config.listen_port({}) == 8000
    assert config.listen_port({"LISTEN_PORT": "nope"}) == 8000
    assert config.listen_port({"LISTEN_PORT": "8813"}) == 8813
    assert config.secret({"UI_PASSWORD": " pw "}, "UI_PASSWORD") == "pw"
    assert config.secret({"UI_PASSWORD": "a", "UI_PASSWORD_FILE": "/nowhere"}, "UI_PASSWORD") == "a"


# Schema


def _read_names() -> set[str]:
    source = (ROOT / "src" / "posteryard" / "config.py").read_text()
    return set(re.findall(r'"([A-Z][A-Z0-9]*_[A-Z0-9_]+|[A-Z]{4,})"', source)) - set(config.REMOVED_NTFY)


def test_every_setting_config_reads_is_in_the_schema() -> None:
    assert sorted(_read_names() - set(BY_KEY) - {"PLEX_LIBRARIES"}) == []
    assert sorted(set(BY_KEY) - _read_names()) == []
    assert {s.group for s in SETTINGS} == set(GROUPS)


def test_secrets_are_secret_fields() -> None:
    assert {s.key for s in SETTINGS if s.type == "secret"} == set(config.SECRETS)


@pytest.mark.parametrize(
    "setting", [s for s in SETTINGS if s.key not in ("DATA_DIR", "TMDB_API_KEY")], ids=lambda s: s.key
)
def test_the_schema_default_is_the_service_default(setting: Any) -> None:
    base = {"TMDB_API_KEY": "example"}
    assert config.load(base | {setting.key: setting.default}) == config.load(base)


@pytest.mark.parametrize(("name", "allowed"), CHOICES.items())
def test_the_schema_offers_every_value_the_service_takes(name: str, allowed: set[str]) -> None:
    assert {option[0] for option in BY_KEY[name].options} == allowed


def test_the_readme_documents_every_setting() -> None:
    readme = (ROOT / "README.md").read_text()
    table = readme.split("\n## Settings\n", 1)[1].split("\n## ", 1)[0]
    assert [s.key for s in SETTINGS if f"| `{s.key}` |" not in table] == []


# Routes


def test_get_shows_where_each_value_comes_from(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_LIBRARIES", "Films")
    config.save_settings(env, {"LOG_LEVEL": "debug", "FANART_API_KEY": "ui-fanart"})
    ui = service_ui(env)
    data = answer(call(ui, "GET", "/api/settings"))
    assert [g["name"] for g in data["groups"]] == list(GROUPS)
    assert (data["restart_required"], data["error"], data["path"]) == (False, None, str(env / "settings.json"))
    found = fields(data)
    assert (found["LOG_LEVEL"]["value"], found["LOG_LEVEL"]["source"]) == ("debug", "ui")
    assert (found["PLEX_URL"]["value"], found["PLEX_URL"]["source"]) == (ENV["PLEX_URL"], "env")
    assert (found["LIBRARIES"]["value"], found["LIBRARIES"]["source"]) == ("Films", "env")
    assert (found["DAILY_AT"]["value"], found["DAILY_AT"]["source"]) == ("04:15", "default")
    assert (found["DATA_DIR"]["value"], found["DATA_DIR"]["readonly"]) == (str(env), True)
    assert found["NOTIFY_EVENTS"]["min"] == 1
    assert found["QUALITY_MIN_VIDEO"]["options"][1] == ["720", "720p", "HD badge"]
    secrets = {k: (f["value"], f["is_set"], f["source"]) for k, f in found.items() if f["type"] == "secret"}
    assert secrets["FANART_API_KEY"] == ("", True, "ui")
    assert secrets["PLEX_TOKEN"] == ("", True, "env")
    assert secrets["UI_PASSWORD"] == ("", False, "default")
    text = json.dumps(data)
    assert not [s for s in ("ui-fanart", "env-token", "env-tmdb", "env-secret") if s in text]


def test_a_secret_file_counts_as_set(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (env / "key").write_text("from-file")
    monkeypatch.setenv("FANART_API_KEY_FILE", str(env / "key"))
    found = fields(answer(call(service_ui(env), "GET", "/api/settings")))
    assert (found["FANART_API_KEY"]["is_set"], found["FANART_API_KEY"]["source"]) == (True, "env")


def test_put_saves_and_asks_for_a_restart(env: Path) -> None:
    ui = service_ui(env)
    response = call(ui, "PUT", "/api/settings", {"values": {"LOG_LEVEL": "debug", "FANART_API_KEY": " new-key "}})
    assert response.status == 200
    data = answer(response)
    assert data["restart_required"] and ui.restart_required
    assert config.saved_settings(env) == {"LOG_LEVEL": "debug", "FANART_API_KEY": "new-key"}
    assert "new-key" not in response.body.decode()
    assert "Set-Cookie" not in response.headers


def test_put_keeps_a_secret_sent_empty_and_drops_a_value_sent_as_null(env: Path) -> None:
    config.save_settings(env, {"FANART_API_KEY": "kept", "LOG_LEVEL": "debug"})
    ui = service_ui(env)
    assert call(ui, "PUT", "/api/settings", {"values": {"FANART_API_KEY": "", "LOG_LEVEL": None}}).status == 200
    assert config.saved_settings(env) == {"FANART_API_KEY": "kept"}
    assert fields(answer(call(ui, "GET", "/api/settings")))["LOG_LEVEL"]["source"] == "default"


def test_put_does_not_save_values_that_are_already_in_use(env: Path) -> None:
    ui = service_ui(env)
    values = {"PLEX_URL": ENV["PLEX_URL"], "DAILY_AT": "04:15", "SWEEP_MINUTES": "30", "NOTIFY_URLS": ""}
    assert call(ui, "PUT", "/api/settings", {"values": values}).status == 200
    assert config.saved_settings(env) == {"SWEEP_MINUTES": "30"}
    # A value saved before stays saved, even when it is the default again.
    assert call(ui, "PUT", "/api/settings", {"values": {"SWEEP_MINUTES": "15"}}).status == 200
    assert config.saved_settings(env) == {"SWEEP_MINUTES": "15"}


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"DAILY_AT": "25:00"}, "DAILY_AT must be a 24-hour time"),
        ({"PLEX_URL": ""}, "PLEX_URL and PLEX_TOKEN are both required"),
        ({"JELLYFIN_URL": "http://jf.example"}, "Set one media server only"),
        ({"NOPE": "1"}, "NOPE is not a setting"),
        ({"DATA_DIR": "/x"}, "DATA_DIR can only be set in the compose file"),
        ({"LOG_LEVEL": 3}, "LOG_LEVEL must be text or null"),
    ],
)
def test_put_refuses_settings_that_do_not_load(env: Path, values: dict[str, Any], message: str) -> None:
    ui = service_ui(env)
    response = call(ui, "PUT", "/api/settings", {"values": values})
    assert response.status == 400
    assert message in answer(response)["error"]
    assert not (env / "settings.json").exists()
    assert not ui.restart_required


def test_put_refuses_a_body_without_an_object(env: Path) -> None:
    assert call(service_ui(env), "PUT", "/api/settings", {"values": ["LOG_LEVEL"]}).status == 400


def test_put_that_cannot_write_answers_with_the_reason(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(folder: Path, values: Any) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(config, "save_settings", fail)
    response = call(service_ui(env), "PUT", "/api/settings", {"values": {"LOG_LEVEL": "debug"}})
    assert (response.status, answer(response)["error"]) == (
        500,
        f"Could not save {env}/settings.json: Permission denied",
    )


def test_settings_need_a_session_once_there_is_a_password(env: Path) -> None:
    ui = service_ui(env)
    for method, path in (("GET", "/api/settings"), ("PUT", "/api/settings"), ("POST", "/api/settings/test")):
        body = {"values": {"UI_PASSWORD": "mine"}, "what": "tmdb"}
        assert call(ui, method, path, body, signed_in=False).status == 401
    assert not (env / "settings.json").exists()


def test_a_new_password_applies_at_once(env: Path) -> None:
    ui = service_ui(env)
    old = ui.auth.issue()
    response = call(ui, "PUT", "/api/settings", {"values": {"UI_PASSWORD": "new-password"}})
    assert response.status == 200
    assert ui.auth.check_password("new-password") and not ui.auth.valid(old)
    token = response.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    assert ui.auth.valid(token)


# Connection checks


def check(ui: WebUi, what: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
    response = call(ui, "POST", "/api/settings/test", {"what": what, "values": values or {}})
    assert response.status == 200
    data: dict[str, Any] = answer(response)
    return data


class Sections(FakeServer):
    def __init__(self, *args: Any) -> None:
        self.args = args

    def sections(self) -> list[Item]:
        return [Item(key="1", title="Movies", type="movie"), Item(key="2", title="Shows", type="show")]


def test_the_server_check_uses_the_values_given(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Sections] = []

    def plex(url: str, token: str) -> Sections:
        made.append(Sections(url, token))
        return made[-1]

    monkeypatch.setattr(cli, "Plex", plex)
    ui = service_ui(env)
    result = check(ui, "server", {"PLEX_TOKEN": "typed", "LIBRARIES": "Movies,TV Shows"})
    assert result == {"ok": True, "message": "Plex answered. Its libraries: Movies, Shows. Not found: TV Shows."}
    assert made[0].args == (ENV["PLEX_URL"], "typed")
    assert check(ui, "server", {"LIBRARIES": "Anime"})["ok"] is False
    assert check(ui, "server", {"PLEX_TOKEN": None, "PLEX_URL": ""}) == {
        "ok": False,
        "message": "PLEX_URL and PLEX_TOKEN are both required",
    }


def test_the_server_check_works_without_a_tmdb_key(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_API_KEY")
    monkeypatch.setenv("TMDB_API_KEY_FILE", str(env / "missing"))
    monkeypatch.setattr(cli, "Plex", lambda url, token: Sections())
    ui = WebUi(env, "pw", setup_error="TMDB_API_KEY is required")
    assert check(ui, "server")["ok"] is True


def test_the_tmdb_and_fanart_checks(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Tmdb:
        def __init__(self, key: str) -> None:
            assert key == "env-tmdb"

        def details(self, kind: str, tmdb_id: int) -> dict[str, Any]:
            return {"title": "The Matrix"}

    class Fanart:
        def __init__(self, key: str) -> None:
            self.key = key

        def images(self, kind: str, tmdb_id: int, tvdb_id: int | None) -> None:
            if self.key != "good":
                raise http.HttpError(401, "https://webservice.fanart.tv/v3/movies/603", "Invalid API key")

    monkeypatch.setattr(api_settings, "Tmdb", Tmdb)
    monkeypatch.setattr(api_settings, "Fanart", Fanart)
    ui = service_ui(env)
    assert check(ui, "tmdb") == {"ok": True, "message": "TMDB answered with The Matrix."}
    assert check(ui, "fanart") == {"ok": False, "message": "Set the fanart.tv API key first"}
    assert check(ui, "fanart", {"FANART_API_KEY": "good"}) == {"ok": True, "message": "fanart.tv answered."}
    failed = check(ui, "fanart", {"FANART_API_KEY": "bad"})
    assert failed["ok"] is False and "HTTP 401" in failed["message"]


def test_the_maintainerr_check(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Maintainerr:
        def __init__(self, url: str) -> None:
            assert url == "http://maintainerr.example:6246"

        def action_days(self) -> dict[str, Any]:
            return {"1": None, "2": None}

    monkeypatch.setattr(api_settings, "Maintainerr", Maintainerr)
    ui = service_ui(env)
    assert check(ui, "maintainerr")["message"] == "Set the Maintainerr address first"
    result = check(ui, "maintainerr", {"MAINTAINERR_URL": "http://maintainerr.example:6246/"})
    assert result == {"ok": True, "message": "Maintainerr answered. 2 titles have a deletion date."}


def test_the_notify_check(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []

    class Notifier:
        def __init__(self, urls: list[str]) -> None:
            self.urls = urls

        def send(self, subject: str, message: str) -> bool:
            sent.append((subject, message))
            return self.urls == ["json://alerts.example"]

    monkeypatch.setattr(notify, "Notifier", Notifier)
    ui = service_ui(env)
    assert check(ui, "notify")["ok"] is False
    assert check(ui, "notify", {"NOTIFY_URLS": "nope://x"})["message"] == "Address 1 is not one Apprise supports"
    assert check(ui, "notify", {"NOTIFY_URLS": "json://alerts.example"}) == {"ok": True, "message": "Test alert sent."}
    assert check(ui, "notify", {"NOTIFY_URLS": "json://other.example"})["ok"] is False
    assert sent[0] == ("test", "Alerts from Posteryard reach you.")


def test_an_unknown_check_is_refused(env: Path) -> None:
    response = call(service_ui(env), "POST", "/api/settings/test", {"what": "plex"})
    assert response.status == 400
    assert "server, tmdb, fanart, maintainerr, notify" in answer(response)["error"]


def test_a_slow_or_broken_check_is_an_answer(caplog: pytest.LogCaptureFixture) -> None:
    def slow(env: dict[str, str]) -> tuple[bool, str]:
        time.sleep(0.5)
        return True, "late"

    def broken(env: dict[str, str]) -> tuple[bool, str]:
        raise RuntimeError("boom")

    assert api_settings.within(0.05, slow, {}) == (False, "No answer within 0 seconds")
    assert api_settings.within(1, broken, {}) == (False, "RuntimeError: boom")
    assert "settings check failed" in caplog.text
