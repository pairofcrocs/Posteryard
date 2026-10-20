"""Web UI API: missing logos, textless posters and collection movies. See docs/webui-api.md."""

from typing import TYPE_CHECKING

from posteryard.webui.missing import MissingScan
from posteryard.webui.routing import ApiError, Request, Response, json_response

if TYPE_CHECKING:
    from posteryard.webui.app import WebUi


def register(ui: "WebUi", scan: MissingScan | None = None) -> None:
    service = ui.service
    assert service is not None
    # Safe to share with the worker: TMDB locks its cache, and fanart.tv keeps no state.
    sources = service.worker.ctx.sources
    found = scan or MissingScan(service.cfg, service.server, service.store, tmdb=sources.tmdb, fanart=sources.fanart)
    r = ui.router

    @r.get("/api/missing")
    def report(_req: Request) -> Response:
        return json_response(found.report())

    @r.post("/api/missing/scan")
    def start(_req: Request) -> Response:
        if not found.start():
            raise ApiError(409, "A check is already running")
        return json_response({"ok": True})
