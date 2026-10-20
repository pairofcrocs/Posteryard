"""The web UI: a JSON API under /api and a single-page app for every other path."""

import logging
import mimetypes
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from posteryard import __version__
from posteryard.webui import logbuffer
from posteryard.webui.auth import COOKIE, Auth
from posteryard.webui.jobs import Job, Jobs
from posteryard.webui.routing import ApiError, Request, Response, Router, error, json_response, parse_query

if TYPE_CHECKING:
    from posteryard.config import Config
    from posteryard.service import Service

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_API_BODY = 40 * 1024 * 1024
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "posteryard"
SAFE = frozenset({"GET", "HEAD"})
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob: https:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class _Headers(dict[str, str]):
    """Header names match whatever their case, as in http.server."""

    def __init__(self, raw: dict[str, str]) -> None:
        super().__init__({name.lower(): value for name, value in raw.items()})

    def get(self, name: str, default: str = "") -> str:  # type: ignore[override]
        return super().get(name.lower(), default)


class WebUi:
    """Serves the web UI. `service` is None in setup mode, when the settings do not load yet."""

    def __init__(
        self,
        data_dir: Path,
        password: str,
        *,
        cfg: "Config | None" = None,
        service: "Service | None" = None,
        setup_error: str | None = None,
    ) -> None:
        self.data_dir, self.cfg, self.service, self.setup_error = data_dir, cfg, service, setup_error
        self.auth = Auth(data_dir, password)
        self.jobs = Jobs(service.submit_job if service is not None else None)
        self.logs = logbuffer.install()
        self.router = Router()
        self.restart_required = False
        self.on_restart: Callable[[], None] | None = service.request_restart if service is not None else None
        self._core_routes()
        # Imported here: the API modules import this one for the WebUi type.
        from posteryard.webui import api_items, api_settings, api_status  # noqa: PLC0415

        api_settings.register(self)
        if service is not None:
            api_status.register(self)
            api_items.register(self)

    @property
    def setup(self) -> bool:
        return self.service is None

    def submit(self, kind: str, label: str, run: Callable[[], Any]) -> Response:
        """Queue work for the worker thread and answer 202 with the job id the page polls."""
        job = self.jobs.submit(kind, label, run)
        return json_response({"job": job.id}, 202)

    def _core_routes(self) -> None:
        r = self.router

        @r.get("/api/session", public=True)
        def session(req: Request) -> Response:
            return json_response({
                "authenticated": self.authenticated(req),
                "password_set": self.auth.enabled,
                "setup": self.setup,
                "setup_error": self.setup_error,
                "version": __version__,
            })  # fmt: skip

        @r.post("/api/login", public=True)
        def login(req: Request) -> Response:
            if self.auth.blocked(req.client):
                raise ApiError(429, "Too many wrong passwords. Wait 5 minutes.")
            password = str(req.json().get("password", ""))
            if not self.auth.check_password(password):
                self.auth.failed(req.client)
                log.warning("web UI sign-in refused", extra={"client": req.client})
                raise ApiError(401, "Wrong password")
            secure = req.headers.get("X-Forwarded-Proto", "").lower() == "https"
            cookie = self.auth.cookie(self.auth.issue(), secure=secure)
            return json_response({"authenticated": True}, headers={"Set-Cookie": cookie})

        @r.post("/api/logout", public=True)
        def logout(_req: Request) -> Response:
            return json_response({"authenticated": False}, headers={"Set-Cookie": self.auth.clear_cookie()})

        @r.get("/api/jobs")
        def jobs(req: Request) -> Response:
            return json_response({"jobs": [job.public() for job in self.jobs.recent(req.int_arg("limit", 50, 1, 200))]})

        @r.get("/api/jobs/{job_id}")
        def job(req: Request) -> Response:
            found: Job | None = self.jobs.get(req.params["job_id"])
            if found is None:
                raise ApiError(404, "no such job")
            return json_response(found.public())

    def authenticated(self, req: Request) -> bool:
        return self.auth.valid(req.cookie(COOKIE))

    def dispatch(self, req: Request) -> Response:
        if not req.path.startswith("/api/"):
            return self.static(req.path) if req.method in SAFE else error(404, "not found")
        try:
            found = self.router.match(req.method, req.path)
            if found is None:
                return error(404, "not found")
            route, req.params = found
            if req.method not in SAFE and req.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return error(403, f"missing the {CSRF_HEADER} header")
            if not route.public and not self.authenticated(req):
                return error(401, "sign in first")
            return route.handler(req)
        except ApiError as exc:
            return error(exc.status, exc.message)
        except Exception as exc:
            log.exception("web UI request failed", extra={"path": req.path})
            return error(500, f"{type(exc).__name__}: {exc}")

    def static(self, path: str) -> Response:
        """Files under /static, and index.html for every other path so the app's own routes load."""
        if path.startswith("/static/"):
            target = (STATIC / path.removeprefix("/static/")).resolve()
            if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                return error(404, "not found")
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
                kind += "; charset=utf-8"
            return Response(200, target.read_bytes(), kind, {"Cache-Control": "no-cache"})
        index = (STATIC / "index.html").read_bytes()
        return Response(200, index, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})

    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        """Answer one request on the service's HTTP server."""
        path, _, query = handler.path.partition("?")
        raw_length = handler.headers.get("Content-Length") or "0"
        length = int(raw_length) if raw_length.isascii() and raw_length.isdigit() else -1
        if length < 0 or length > MAX_API_BODY or handler.headers.get("Transfer-Encoding"):
            response = error(413, "bad size")
        else:
            body = handler.rfile.read(length) if length else b""
            headers = dict(handler.headers.items())
            request = Request(
                method, path, parse_query(query), _Headers(headers), body, client=handler.client_address[0]
            )
            response = self.dispatch(request)
        handler.send_response(response.status)
        handler.send_header("Content-Type", response.content_type)
        handler.send_header("Content-Length", str(len(response.body)))
        for name, value in {**SECURITY_HEADERS, **response.headers}.items():
            handler.send_header(name, value)
        if response.content_type.startswith("application/json"):
            handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if method != "HEAD":
            handler.wfile.write(response.body)
