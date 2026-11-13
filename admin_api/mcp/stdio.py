"""stdio transport: ``manage.py mcp_serve``. Imports the SDK."""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.db import close_old_connections
from django.test import RequestFactory

from ..auth import get_token_model
from ..writes import capture_messages
from .server import build_server

try:
    import anyio
    from mcp.server.stdio import stdio_server
except ImportError as exc:  # pragma: no cover
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        'The MCP transports need the official MCP SDK: pip install "django-admin-api[mcp]"'
    ) from exc


class StdioRequestFactory:
    """Builds a fresh Django request per MCP message.

    With ``raw_token`` the token is looked up on every call, so revoking or expiring it takes
    effect immediately and its scopes apply. With ``user`` (development only) there is no token.
    """

    def __init__(self, mcp_site, *, raw_token: str | None = None, user=None):
        self.mcp_site = mcp_site
        self.raw_token = raw_token
        self.user = user
        self._factory = RequestFactory()

    def __call__(self):
        close_old_connections()  # long-running process: drop stale/broken DB connections
        request = self._factory.post("/mcp/", content_type="application/json")
        request.user = AnonymousUser()
        if self.raw_token is not None:
            token = get_token_model().objects.get_for_raw(self.raw_token)
            if token is not None and token.user.is_active:
                token.touch()
                request.api_token = token
                request.user = token.user
        elif self.user is not None:
            request.user = self.user
        capture_messages(request)  # admin code may call messages.* even on reads
        return self.mcp_site.prepare_request(request)


def serve(mcp_site, request_factory) -> None:
    """Serve ``mcp_site`` on stdin/stdout until the client disconnects."""
    server = build_server(mcp_site, request_factory)

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(main)
