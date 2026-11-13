"""The MCP server built on the official ``mcp`` SDK (low-level ``Server``).

This module and :mod:`.bridge` are the only places that import the SDK. Tools run through
:meth:`AdminMcpSite.call_tool` in a sync thread (``sync_to_async(thread_sensitive=True)``), so
when the transport is a sync Django view they run on the view's own thread and DB connection.
"""

from __future__ import annotations

import contextvars
import json

from django.core.exceptions import ImproperlyConfigured

try:
    import mcp_types as types
    from asgiref.sync import sync_to_async
    from mcp.server import Server
except ImportError as exc:  # pragma: no cover - exercised without the extra installed
    raise ImproperlyConfigured(
        'The MCP transports need the official MCP SDK: pip install "django-admin-api[mcp]"'
    ) from exc

# The Django request of the current MCP message (HTTP: the view's request; stdio: one per call).
current_request: contextvars.ContextVar = contextvars.ContextVar("admin_api_mcp_request")


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("django-admin-api")
    except PackageNotFoundError:  # pragma: no cover
        return "0"


def _tool(spec) -> types.Tool:
    data = {
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "inputSchema": spec.input_schema,
    }
    if spec.output_schema is not None:
        data["outputSchema"] = spec.output_schema
    if spec.annotations:
        data["annotations"] = {"title": spec.title, **spec.annotations}
    return types.Tool.model_validate(data)


def build_server(mcp_site, request_factory=None) -> Server:
    """An SDK ``Server`` whose tools are ``mcp_site``'s.

    ``request_factory()`` returns the Django request for a message; by default the request
    stored in :data:`current_request` by the HTTP view.
    """
    get_request = request_factory or current_request.get

    async def on_list_tools(ctx, params):
        request = await sync_to_async(get_request, thread_sensitive=True)()
        return types.ListToolsResult(tools=[_tool(spec) for spec in mcp_site.get_tools(request)])

    def _call(name, arguments):
        request = get_request()
        return mcp_site.call_tool(request, name, arguments)

    async def on_call_tool(ctx, params):
        result = await sync_to_async(_call, thread_sensitive=True)(params.name, params.arguments)
        text = json.dumps(result.data, ensure_ascii=False, separators=(",", ":"))
        return types.CallToolResult(
            content=[types.TextContent(text=text)],
            structured_content=result.data,
            is_error=result.is_error,
        )

    return Server(
        mcp_site.server_name,
        version=_version(),
        instructions=mcp_site.instructions,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
