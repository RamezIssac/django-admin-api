"""``/mcp/``: the Streamable HTTP endpoint (stateless, JSON responses).

Authentication happens here, in Django, before the SDK sees the request: the site's
authenticators (API tokens by default; sessions are not accepted), then
``api_site.has_permission``. The MCP spec requires validating ``Origin`` against DNS
rebinding; a browser ``Origin`` must be the request's own host or in ``CSRF_TRUSTED_ORIGINS``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from asgiref.sync import async_to_sync
from django.conf import settings
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from ..errors import ApiError, PermissionDeniedError
from . import bridge
from .server import build_server


def _origin_allowed(request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # non-browser clients
    if origin in getattr(settings, "CSRF_TRUSTED_ORIGINS", ()):
        return True
    return urlsplit(origin).netloc.lower() == request.get_host().lower()


def _error(exc: ApiError, **headers) -> JsonResponse:
    response = exc.as_response()
    for key, value in headers.items():
        response[key] = value
    return response


def make_view(mcp_site):
    server = build_server(mcp_site)

    @csrf_exempt
    def mcp_view(request):
        request.is_api = True
        if request.method != "POST":
            # Stateless server: no SSE stream to GET, no session to DELETE.
            response = _error(ApiError("Use POST.", status=405, code="method_not_allowed"))
            response["Allow"] = "POST"
            return response
        if not _origin_allowed(request):
            return _error(PermissionDeniedError("Origin not allowed.", code="origin_not_allowed"))
        try:
            mcp_site.authenticate(request)
        except ApiError as exc:
            return _error(exc, **{"WWW-Authenticate": 'Bearer realm="mcp"'})
        if not mcp_site.api_site.has_permission(request):
            return _error(PermissionDeniedError())
        mcp_site.prepare_request(request)
        return async_to_sync(bridge.handle)(server, request)

    return mcp_view


def mcp_urlpatterns(mcp_site):
    return [path("", make_view(mcp_site), name="endpoint")]
