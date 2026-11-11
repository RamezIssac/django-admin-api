"""Run the SDK's Streamable HTTP transport inside a (sync) Django view.

A fresh stateless ``StreamableHTTPSessionManager`` handles each request with plain JSON
responses (no SSE, no sessions). The Django request is translated into an ASGI scope; the
ASGI response is collected into an ``HttpResponse``.
"""

from __future__ import annotations

from django.http import HttpResponse

from .server import current_request

try:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
except ImportError as exc:  # pragma: no cover
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        'The MCP transports need the official MCP SDK: pip install "django-admin-api[mcp]"'
    ) from exc


def _scope(request, body: bytes) -> dict:
    host = request.get_host()
    name, _, port = host.rpartition(":") if host.rsplit(":", 1)[-1].isdigit() else (host, "", "")
    headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in request.headers.items()
        if key.lower() != "content-length"
    ]
    headers.append((b"content-length", str(len(body)).encode("latin-1")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": "https" if request.is_secure() else "http",
        "path": request.path,
        "raw_path": request.path.encode("utf-8"),
        "root_path": "",
        "query_string": request.META.get("QUERY_STRING", "").encode("latin-1"),
        "headers": headers,
        "client": (request.META.get("REMOTE_ADDR") or "", 0),
        "server": (name or host, int(port) if port else (443 if request.is_secure() else 80)),
    }


async def handle(server, request) -> HttpResponse:
    """Serve one Django request with ``server`` (an SDK ``Server``)."""
    current_request.set(request)
    body = request.body
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status, headers, chunks = 500, [], []

    async def send(message):
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status, headers = message["status"], message.get("headers", [])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)
    async with manager.run():
        await manager.handle_request(_scope(request, body), receive, send)

    response = HttpResponse(b"".join(chunks), status=status)
    for key, value in headers:
        if key.lower() not in (b"content-length",):
            response[key.decode("latin-1")] = value.decode("latin-1")
    return response
