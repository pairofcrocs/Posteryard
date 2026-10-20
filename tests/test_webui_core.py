import io
import json
import logging
import os
import stat
from email.message import Message
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast

import pytest

from posteryard import config
from posteryard.service import Service
from posteryard.store import Store
from posteryard.webui import app, auth, jobs, logbuffer, routing
from posteryard.webui.app import WebUi, _Headers
from posteryard.webui.auth import COOKIE, Auth
from posteryard.webui.jobs import Job, JobError, Jobs, JobState
from posteryard.webui.logbuffer import LogBuffer
from posteryard.webui.routing import ApiError, Request, Response, Router, error, json_response, parse_query
from tests import fakes
from tests.fakes import FakeServer

PASSWORD = "example-password"


def make_service(
    tmp_path: Path, server: FakeServer | None = None, env: dict[str, str] | None = None, *, sync: bool = True
) -> Service:
    """A service with its web UI. With `sync`, jobs run at once instead of on the worker thread."""
    cfg = config.load({
        "TMDB_API_KEY": "example", "DATA_DIR": str(tmp_path), "WEBHOOK_SECRET": "example-secret",
        "UI_PASSWORD": PASSWORD, **(env or {}),
    })  # fmt: skip
    server = server or FakeServer()
    store = Store(cfg.state_path)
    service = Service(cfg, server, store, fakes.worker(cfg, server, store))
    service.attach_ui()
    if sync and service.ui is not None:
        service.ui.jobs.dispatch = service.ui.jobs.execute
    return service


def call(  # noqa: PLR0913
    ui: WebUi,
    method: str,
    path: str,
    body: object = None,
    *,
    signed_in: bool = True,
    csrf: bool = True,
    headers: dict[str, str] | None = None,
    client: str = "127.0.0.1",
) -> Response:
    sent = dict(headers or {})
    if signed_in:
        sent["Cookie"] = f"{COOKIE}={ui.auth.issue()}"
    if csrf and method not in ("GET", "HEAD"):
        sent["X-Requested-With"] = "posteryard"
    raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
    path, _, query = path.partition("?")
    return ui.dispatch(Request(method, path, parse_query(query), _Headers(sent), raw, client=client))


def answer(response: Response) -> Any:
    return json.loads(response.body)


# Routing


def handler(name: str) -> routing.Handler:
    return lambda req: json_response({"name": name, "params": req.params})


def test_routes_match_with_their_path_params() -> None:
    router = Router()
    router.get("/api/items/{key}")(handler("item"))
    router.add("GET", "/api/files/{rest:path}", handler("file"))
    router.post("/api/items/{key}/render")(handler("render"))
    router.put("/api/settings", public=True)(handler("settings"))
    router.delete("/api/previews")(handler("previews"))
    found = router.match("GET", "/api/items/12")
    assert found is not None and found[1] == {"key": "12"} and not found[0].public
    assert router.match("GET", "/api/items/12/more") is None
    found = router.match("GET", "/api/files/a/b/c.jpg")
    assert found is not None and found[1] == {"rest": "a/b/c.jpg"}
    head = router.match("HEAD", "/api/items/3")
    assert head is not None and head[0].method == "GET"
    found = router.match("PUT", "/api/settings")
    assert found is not None and found[0].public
    assert router.match("DELETE", "/api/previews") is not None
    assert router.match("GET", "/api/nothing") is None


def test_a_path_with_another_method_only_is_405() -> None:
    router = Router()
    router.post("/api/actions/sweep")(handler("sweep"))
    with pytest.raises(ApiError) as caught:
        router.match("GET", "/api/actions/sweep")
    assert caught.value.status == 405


def test_the_query_keeps_the_last_value_and_blanks() -> None:
    assert parse_query("a=1&a=2&b=&c=%20x") == {"a": "2", "b": "", "c": " x"}
    assert parse_query("") == {}


def request(body: bytes = b"", query: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Request:
    return Request("POST", "/api/x", query or {}, headers or {}, body)


def test_request_json() -> None:
    assert request().json() == {}
    assert request(b'{"a": 1}').json() == {"a": 1}
    for bad, message in [(b"{", "not valid JSON"), (b"[1]", "JSON object"), (b"[" * 100_000, "not valid JSON")]:
        with pytest.raises(ApiError, match=message) as caught:
            request(bad).json()
        assert caught.value.status == 400


def test_int_args_are_checked_and_clamped() -> None:
    args = request(query={"a": "", "b": "abc", "c": "-5", "d": "99999", "e": "7", "f": "٣"})
    assert args.int_arg("a", 3) == 3
    assert args.int_arg("missing", 4) == 4
    assert args.int_arg("c", 0, 1, 10) == 1
    assert args.int_arg("d", 0, 1, 100) == 100
    assert args.int_arg("e", 0) == 7
    for name in ("b", "f"):
        with pytest.raises(ApiError, match="whole number"):
            args.int_arg(name, 0)


def test_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    jar = request(headers={"Cookie": "a=1; posteryard_session=abc.def"})
    assert jar.cookie("posteryard_session") == "abc.def"
    assert jar.cookie("missing") is None
    assert request().cookie("a") is None

    def broken(self: object, raw: str) -> None:
        raise ValueError(raw)

    monkeypatch.setattr(SimpleCookie, "load", broken)
    assert jar.cookie("a") is None


def test_responses() -> None:
    response = json_response({"when": Path("/x")}, 201, {"X-Test": "1"})
    assert (response.status, answer(response), response.headers) == (201, {"when": "/x"}, {"X-Test": "1"})
    assert answer(error(418, "no")) == {"error": "no"} and error(418, "no").status == 418


# Auth


def test_a_cookie_is_valid_until_it_expires(tmp_path: Path) -> None:
    signer = Auth(tmp_path, PASSWORD)
    token = signer.issue(now=1000)
    assert signer.valid(token, now=1001)
    assert not signer.valid(token, now=1000 + auth.SESSION_SECONDS + 1)
    assert signer.valid(signer.issue())
    assert not signer.valid(None) and not signer.valid("")


def test_a_tampered_cookie_is_refused(tmp_path: Path) -> None:
    signer = Auth(tmp_path, PASSWORD)
    expiry, _, signature = signer.issue().partition(".")
    assert not signer.valid(f"{int(expiry) + 1000}.{signature}")
    assert not signer.valid(f"{expiry}.{'0' * len(signature)}")
    assert not signer.valid(f"x{expiry}.{signature}")
    assert not signer.valid(expiry)


def test_a_new_password_signs_everyone_out(tmp_path: Path) -> None:
    token = Auth(tmp_path, PASSWORD).issue()
    assert Auth(tmp_path, PASSWORD).valid(token)
    assert not Auth(tmp_path, "another-password").valid(token)


def test_without_a_password_nobody_signs_in(tmp_path: Path) -> None:
    signer = Auth(tmp_path, "")
    assert not signer.enabled
    assert not signer.check_password("")
    assert not signer.valid(signer.issue())


def test_the_password_check(tmp_path: Path) -> None:
    signer = Auth(tmp_path, PASSWORD)
    assert signer.enabled and signer.check_password(PASSWORD)
    assert not signer.check_password("wrong")


def test_the_key_file_is_private_and_reused(tmp_path: Path) -> None:
    data = tmp_path / "new"
    Auth(data, PASSWORD)
    key = data / auth.KEY_FILE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    first = key.read_bytes()
    assert len(first) == 32
    Auth(data, PASSWORD)
    assert key.read_bytes() == first


def test_an_unwritable_data_folder_gives_a_key_per_run(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    token = Auth(blocked, PASSWORD).issue()
    assert not Auth(blocked, PASSWORD).valid(token)


def test_wrong_passwords_block_a_client_for_a_while(tmp_path: Path) -> None:
    signer = Auth(tmp_path, PASSWORD)
    for n in range(auth.MAX_FAILURES):
        assert not signer.blocked("10.0.0.1", now=100 + n)
        signer.failed("10.0.0.1", now=100 + n)
    assert signer.blocked("10.0.0.1", now=120)
    assert not signer.blocked("10.0.0.2", now=120)
    assert not signer.blocked("10.0.0.1", now=100 + auth.MAX_FAILURES + auth.FAILURE_WINDOW)
    signer.failed("10.0.0.3")
    assert not signer.blocked("10.0.0.3")


def test_the_cookie_flags(tmp_path: Path) -> None:
    signer = Auth(tmp_path, PASSWORD)
    plain = signer.cookie("t")
    assert plain.startswith(f"{COOKIE}=t;") and "HttpOnly" in plain and "SameSite=Strict" in plain
    assert "Secure" not in plain and signer.cookie("t", secure=True).endswith("; Secure")
    assert "Max-Age=0" in Auth.clear_cookie()


# Jobs


def test_a_job_runs_and_keeps_its_result() -> None:
    queue = Jobs()
    seen: list[Job | None] = []

    def run() -> dict[str, int]:
        seen.append(queue.running())
        return {"n": 1}

    job = queue.submit("test", "Test", run)
    assert job.state == JobState.DONE and job.result == {"n": 1}
    assert seen == [job] and queue.running() is None
    assert job.started is not None and job.finished is not None and job.finished >= job.started
    public = job.public()
    assert public["state"] == "done" and public["error"] is None
    assert set(public) == {"id", "kind", "label", "state", "created", "started", "finished", "result", "error"}
    assert queue.get(job.id) is job and queue.get("nope") is None


def test_a_dispatched_job_waits_in_the_queued_state() -> None:
    waiting: list[Job] = []
    queue = Jobs(waiting.append)
    job = queue.submit("test", "Test", lambda: 1)
    assert waiting == [job] and job.state == JobState.QUEUED and job.public()["started"] is None
    queue.execute(job)
    assert job.public()["state"] == "done" and job.result == 1


def test_a_job_error_is_shown_as_is(caplog: pytest.LogCaptureFixture) -> None:
    def refuse() -> None:
        raise JobError("No art found")

    job = Jobs().submit("test", "Test", refuse)
    assert job.state == JobState.FAILED and job.error == "No art found"
    assert "web UI job failed" not in caplog.text


def test_an_unexpected_error_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    def crash() -> None:
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR):
        job = Jobs().submit("test", "Crash", crash)
    assert job.state == JobState.FAILED and job.error == "ValueError: boom" and job.finished is not None
    assert "web UI job failed" in caplog.text


def test_only_the_latest_jobs_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "KEEP", 3)
    queue = Jobs()
    made = [queue.submit("test", str(n), lambda: None) for n in range(5)]
    assert queue.get(made[0].id) is None and queue.get(made[1].id) is None
    assert [job.label for job in queue.recent()] == ["4", "3", "2"]
    assert [job.label for job in queue.recent(2)] == ["4", "3"]
    assert len({job.id for job in made}) == 5


# Log buffer


@pytest.fixture
def buffered() -> Any:
    buffer = LogBuffer(size=50)
    logger = logging.getLogger("tests.webui.buffer")
    logger.addHandler(buffer)
    logger.setLevel(1)
    logger.propagate = False
    yield logger, buffer
    logger.removeHandler(buffer)
    logger.propagate = True


def test_records_keep_their_level_and_fields(buffered: tuple[logging.Logger, LogBuffer]) -> None:
    logger, buffer = buffered
    assert buffer.last() == 0
    logger.debug("quiet")
    logger.info("item processed", extra={"key": "7", "took": 2})
    logger.warning("slow")
    logger.error("bad")
    logger.critical("worse")
    buffer.emit(logging.LogRecord("tests.webui.buffer", 5, __file__, 1, "finer than debug", None, None))
    found = buffer.since()
    assert [r["level"] for r in found] == ["debug", "info", "warning", "error", "error", "debug"]
    assert [r["seq"] for r in found] == [1, 2, 3, 4, 5, 6] and buffer.last() == 6
    info = found[1]
    assert info["msg"] == "item processed" and info["logger"] == "tests.webui.buffer"
    assert info["fields"] == {"key": "7", "took": "2"}
    assert info["time"].endswith("+00:00") and "T" in info["time"]


def test_records_filter_by_level_and_seq(buffered: tuple[logging.Logger, LogBuffer]) -> None:
    logger, buffer = buffered
    for n in range(6):
        (logger.info if n % 2 else logger.warning)(f"line {n}")
    assert [r["msg"] for r in buffer.since(level="warning")] == ["line 0", "line 2", "line 4"]
    assert [r["seq"] for r in buffer.since(after=4)] == [5, 6]
    assert [r["seq"] for r in buffer.since(limit=2)] == [5, 6]
    assert buffer.since(level="error") == []
    assert len(buffer.since(level="nonsense")) == 6


def test_an_exception_becomes_a_field(buffered: tuple[logging.Logger, LogBuffer]) -> None:
    logger, buffer = buffered
    try:
        raise KeyError("x")
    except KeyError:
        logger.exception("failed")
    assert buffer.since()[0]["fields"]["error"] == "KeyError: 'x'"


def test_the_buffer_keeps_the_newest_records() -> None:
    buffer = LogBuffer(size=3)
    logger = logging.getLogger("tests.webui.ring")
    logger.addHandler(buffer)
    logger.propagate = False
    try:
        for n in range(5):
            logger.warning(f"line {n}")
    finally:
        logger.removeHandler(buffer)
        logger.propagate = True
    assert [r["seq"] for r in buffer.since()] == [3, 4, 5] and buffer.last() == 5


def test_a_broken_record_never_raises(
    buffered: tuple[logging.Logger, LogBuffer], monkeypatch: pytest.MonkeyPatch
) -> None:
    _logger, buffer = buffered
    broken: list[logging.LogRecord] = []
    monkeypatch.setattr(buffer, "handleError", broken.append)
    buffer.emit(logging.LogRecord("tests.webui.buffer", logging.INFO, __file__, 1, "%d items", ("many",), None))
    assert len(broken) == 1 and buffer.since() == []


def test_one_buffer_is_installed_on_the_root_logger() -> None:
    first = logbuffer.install()
    assert logbuffer.install() is first
    assert first in logging.getLogger().handlers


# The web UI


@pytest.fixture
def ui(tmp_path: Path) -> WebUi:
    service = make_service(tmp_path)
    assert service.ui is not None
    return service.ui


def test_the_session_says_who_is_signed_in(ui: WebUi) -> None:
    session = answer(call(ui, "GET", "/api/session", signed_in=False))
    assert session["authenticated"] is False and session["password_set"] is True
    assert session["setup"] is False and session["setup_error"] is None and session["version"]
    assert answer(call(ui, "GET", "/api/session"))["authenticated"] is True


def test_sign_in_and_out(ui: WebUi) -> None:
    wrong = call(ui, "POST", "/api/login", {"password": "wrong"}, signed_in=False)
    assert wrong.status == 401 and answer(wrong) == {"error": "Wrong password"}
    right = call(ui, "POST", "/api/login", {"password": PASSWORD}, signed_in=False)
    assert right.status == 200 and answer(right) == {"authenticated": True}
    cookie = right.headers["Set-Cookie"]
    assert "Secure" not in cookie
    token = cookie.split(";")[0].removeprefix(f"{COOKIE}=")
    assert ui.auth.valid(token)
    proxied = call(ui, "POST", "/api/login", {"password": PASSWORD}, headers={"X-Forwarded-Proto": "HTTPS"})
    assert "Secure" in proxied.headers["Set-Cookie"]
    out = call(ui, "POST", "/api/logout", signed_in=False)
    assert answer(out) == {"authenticated": False} and "Max-Age=0" in out.headers["Set-Cookie"]


def test_many_wrong_passwords_are_refused_for_a_while(ui: WebUi) -> None:
    for _ in range(auth.MAX_FAILURES):
        assert call(ui, "POST", "/api/login", {"password": "x"}, signed_in=False, client="10.1.1.1").status == 401
    blocked = call(ui, "POST", "/api/login", {"password": PASSWORD}, signed_in=False, client="10.1.1.1")
    assert blocked.status == 429
    assert call(ui, "POST", "/api/login", {"password": PASSWORD}, signed_in=False, client="10.1.1.2").status == 200


def test_api_routes_need_the_cookie_and_writes_the_header(ui: WebUi) -> None:
    assert call(ui, "GET", "/api/jobs", signed_in=False).status == 401
    assert call(ui, "GET", "/api/jobs", headers={"Cookie": f"{COOKIE}=1.bad"}, signed_in=False).status == 401
    assert call(ui, "GET", "/api/jobs").status == 200
    refused = call(ui, "POST", "/api/login", {"password": PASSWORD}, signed_in=False, csrf=False)
    assert refused.status == 403 and "X-Requested-With" in answer(refused)["error"]
    assert call(ui, "POST", "/api/logout", csrf=False, headers={"X-Requested-With": "other"}).status == 403


def test_unknown_routes_and_methods(ui: WebUi) -> None:
    assert call(ui, "GET", "/api/nothing").status == 404
    assert call(ui, "GET", "/api/login").status == 405
    assert call(ui, "POST", "/library").status == 404
    assert call(ui, "DELETE", "/").status == 404


def test_jobs_routes(ui: WebUi) -> None:
    job = ui.jobs.submit("test", "Test", lambda: {"ok": True})
    listed = answer(call(ui, "GET", "/api/jobs?limit=1"))["jobs"]
    assert [entry["id"] for entry in listed] == [job.id]
    one = answer(call(ui, "GET", f"/api/jobs/{job.id}"))
    assert one["state"] == "done" and one["result"] == {"ok": True}
    assert call(ui, "GET", "/api/jobs/1-999").status == 404
    response = ui.submit("test", "Again", lambda: 2)
    assert response.status == 202 and ui.jobs.get(answer(response)["job"]) is not None


def test_handler_errors_become_answers(ui: WebUi, caplog: pytest.LogCaptureFixture) -> None:
    def refuse(_req: Request) -> Response:
        raise ApiError(409, "busy")

    def crash(_req: Request) -> Response:
        raise RuntimeError("boom")

    ui.router.add("GET", "/api/test/refuse", refuse)
    ui.router.add("GET", "/api/test/crash", crash)
    refused = call(ui, "GET", "/api/test/refuse")
    assert refused.status == 409 and answer(refused) == {"error": "busy"}
    with caplog.at_level(logging.ERROR):
        crashed = call(ui, "GET", "/api/test/crash")
    assert crashed.status == 500 and answer(crashed) == {"error": "RuntimeError: boom"}
    assert "web UI request failed" in caplog.text
    assert call(ui, "POST", "/api/login", b"{", signed_in=False).status == 400


def test_static_files_and_the_app_for_every_other_path(ui: WebUi) -> None:
    css = call(ui, "GET", "/static/css/app.css", signed_in=False)
    assert css.status == 200 and css.content_type == "text/css; charset=utf-8" and css.body
    script = call(ui, "GET", "/static/js/app.js", signed_in=False)
    assert script.content_type.endswith("javascript; charset=utf-8")
    svg = call(ui, "GET", "/static/img/logo.svg", signed_in=False)
    assert svg.content_type == "image/svg+xml; charset=utf-8"
    index = (app.STATIC / "index.html").read_bytes()
    for path in ("/", "/library", "/item/12", "/static"):
        page = call(ui, "GET", path, signed_in=False)
        assert page.status == 200 and page.body == index and page.content_type.startswith("text/html")
    assert call(ui, "HEAD", "/settings", signed_in=False).body == index
    for bad in ("/static/../app.py", "/static/../../service.py", "/static/nope.js", "/static/css"):
        assert call(ui, "GET", bad, signed_in=False).status == 404, bad


def test_an_unknown_file_type_is_sent_as_bytes(ui: WebUi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "data.unknownext").write_bytes(b"\x00")
    monkeypatch.setattr(app, "STATIC", static)
    response = call(ui, "GET", "/static/data.unknownext", signed_in=False)
    assert response.status == 200 and response.content_type == "application/octet-stream"


def test_setup_mode_has_no_service_routes(tmp_path: Path) -> None:
    setup = WebUi(tmp_path, "", setup_error="TMDB_API_KEY is required")
    assert setup.setup and setup.on_restart is None
    session = answer(call(setup, "GET", "/api/session", signed_in=False))
    assert session["setup"] is True and session["setup_error"] == "TMDB_API_KEY is required"
    assert session["password_set"] is False
    assert call(setup, "GET", "/api/status").status == 404


class FakeHandler:
    """Enough of BaseHTTPRequestHandler for WebUi.handle."""

    def __init__(self, path: str, headers: dict[str, str], body: bytes = b"") -> None:
        self.path = path
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name] = value
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = ("192.0.2.1", 50000)
        self.status = 0
        self.sent: dict[str, str] = {}

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, name: str, value: str) -> None:
        self.sent[name] = value

    def end_headers(self) -> None:
        pass


def handle(ui: WebUi, method: str, path: str, headers: dict[str, str], body: bytes = b"") -> FakeHandler:
    fake = FakeHandler(path, headers, body)
    ui.handle(cast(BaseHTTPRequestHandler, fake), method)
    return fake


def test_handle_answers_with_security_headers(ui: WebUi) -> None:
    fake = handle(ui, "GET", "/api/session?x=1", {})
    assert fake.status == 200 and json.loads(fake.wfile.getvalue())["authenticated"] is False
    assert fake.sent["Cache-Control"] == "no-store" and fake.sent["Content-Type"] == "application/json"
    assert fake.sent["Content-Length"] == str(len(fake.wfile.getvalue()))
    for name, value in app.SECURITY_HEADERS.items():
        assert fake.sent[name] == value
    page = handle(ui, "GET", "/library", {})
    assert page.sent["Cache-Control"] == "no-cache" and page.sent["X-Frame-Options"] == "DENY"
    head = handle(ui, "HEAD", "/library", {})
    assert head.status == 200 and head.wfile.getvalue() == b"" and int(head.sent["Content-Length"]) > 0


def test_handle_reads_the_body_and_client(ui: WebUi) -> None:
    body = json.dumps({"password": PASSWORD}).encode()
    headers = {"Content-Length": str(len(body)), "x-requested-with": "posteryard"}
    fake = handle(ui, "POST", "/api/login", headers, body)
    assert fake.status == 200 and fake.sent["Set-Cookie"].startswith(COOKIE)
    for _ in range(auth.MAX_FAILURES):
        handle(ui, "POST", "/api/login", {**headers, "Content-Length": "2"}, b"{}")
    assert ui.auth.blocked("192.0.2.1")


@pytest.mark.parametrize(
    "headers",
    [{"Content-Length": "abc"}, {"Content-Length": str(app.MAX_API_BODY + 1)}, {"Transfer-Encoding": "chunked"}],
)
def test_handle_refuses_a_bad_size(ui: WebUi, headers: dict[str, str]) -> None:
    fake = handle(ui, "POST", "/api/login", headers, b"{}")
    assert fake.status == 413 and json.loads(fake.wfile.getvalue()) == {"error": "bad size"}


def test_the_key_file_is_not_world_readable_even_with_a_loose_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        Auth(tmp_path / "loose", PASSWORD)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "loose" / auth.KEY_FILE).stat().st_mode) == 0o600
