"""A small router for the web UI on top of http.server. Handlers take a Request and return a Response."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs

JSON_TYPE = "application/json"


class ApiError(Exception):
    """Raised by a handler to answer with `{"error": message}` and the given status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: Mapping[str, str]
    body: bytes = b""
    params: dict[str, str] = field(default_factory=dict)
    client: str = ""

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except (ValueError, RecursionError):
            raise ApiError(400, "the body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "the body must be a JSON object")
        return data

    def cookie(self, name: str) -> str | None:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:  # A malformed cookie header is no cookie.
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def int_arg(self, name: str, default: int, low: int = 0, high: int = 10_000) -> int:
        raw = self.query.get(name, "")
        if not raw:
            return default
        if not (raw.isascii() and raw.lstrip("-").isdigit()):
            raise ApiError(400, f"{name} must be a whole number")
        return max(low, min(high, int(raw)))


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = JSON_TYPE
    headers: dict[str, str] = field(default_factory=dict)


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status, json.dumps(data, default=str).encode(), JSON_TYPE, headers or {})


def error(status: int, message: str) -> Response:
    return json_response({"error": message}, status)


Handler = Callable[[Request], Response]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    public: bool


class Router:
    """Routes like `/api/items/{key}`. A `{name}` matches one path segment; `{name:path}` matches the rest."""

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, path: str, handler: Handler, *, public: bool = False) -> None:
        def segment(match: re.Match[str]) -> str:
            name, _, kind = match.group(1).partition(":")
            return f"(?P<{name}>.+)" if kind == "path" else f"(?P<{name}>[^/]+)"

        pattern = re.compile("^" + re.sub(r"\{([^}]+)\}", segment, path) + "$")
        self.routes.append(Route(method.upper(), pattern, handler, public))

    def route(self, method: str, path: str, *, public: bool = False) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.add(method, path, handler, public=public)
            return handler

        return register

    def get(self, path: str, *, public: bool = False) -> Callable[[Handler], Handler]:
        return self.route("GET", path, public=public)

    def post(self, path: str, *, public: bool = False) -> Callable[[Handler], Handler]:
        return self.route("POST", path, public=public)

    def put(self, path: str, *, public: bool = False) -> Callable[[Handler], Handler]:
        return self.route("PUT", path, public=public)

    def delete(self, path: str, *, public: bool = False) -> Callable[[Handler], Handler]:
        return self.route("DELETE", path, public=public)

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        """The route for the request. Raises ApiError(405) when the path exists for another method only."""
        allowed = False
        for route in self.routes:
            found = route.pattern.match(path)
            if not found:
                continue
            if route.method == method or (method == "HEAD" and route.method == "GET"):
                return route, found.groupdict()
            allowed = True
        if allowed:
            raise ApiError(405, "method not allowed")
        return None


def parse_query(raw: str) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
