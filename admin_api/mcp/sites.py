from __future__ import annotations

import logging
from weakref import WeakSet

from django.core.exceptions import ImproperlyConfigured

from ..auth import TokenAuthenticator
from ..errors import ApiError, NotAuthenticated, PermissionDeniedError
from .tools import ToolResult, ToolSpec

logger = logging.getLogger("admin_api.mcp")

_all_sites: WeakSet = WeakSet()


def all_mcp_sites():
    return list(_all_sites)


DEFAULT_INSTRUCTIONS = """\
This server exposes a Django admin. You act as the authenticated user, with exactly their \
permissions; validation and business rules are the admin's own.
1. Call list_models to see what you can access.
2. Call describe_model before filtering or writing: it lists filter parameters, sortable \
columns, actions and the JSON Schema for create/update.
3. Prefer dry_run=true on create_object/update_object before the real write.
4. delete_object only deletes with confirm=true; without it you get a preview.
5. Some models have custom views (reports, statements, computed pages, business operations), listed under 'views' by list_models and described (parameters) by describe_model. Run them with run_view; some are also published as their own tools.
Errors come back with the same envelope as the REST API (error, detail, fields, \
non_field_errors, inlines)."""


class AdminMcpSite:
    """Exposes an :class:`admin_api.AdminApiSite` as an MCP server.

    Same admins, permissions, token scopes and write pipeline as the REST API. Hide an admin
    from MCP only with ``mcp_enabled = False``; give the LLM context with ``mcp_description``.

    The tool layer has no dependency on the MCP SDK; the transports (``urls`` for Streamable
    HTTP, ``manage.py mcp_serve`` for stdio) need ``pip install "django-admin-api[mcp]"``.
    """

    default_per_page = 25
    instructions = DEFAULT_INSTRUCTIONS
    # Bearer tokens only: MCP clients are not browsers, and a session cookie must not be enough.
    authenticators = [TokenAuthenticator]

    def __init__(
        self,
        api_site,
        *,
        name: str = "admin_mcp",
        read_only: bool = False,
        server_name: str | None = None,
        instructions: str | None = None,
    ):
        self.api_site = api_site
        self.name = name
        self.read_only = read_only
        self.server_name = server_name or name
        if instructions is not None:
            self.instructions = instructions
        _all_sites.add(self)

    # --- policy hooks (override in a subclass) -------------------------------------------------
    def is_exposed(self, model_admin) -> bool:
        return self.api_site.is_exposed(model_admin) and getattr(model_admin, "mcp_enabled", True) is not False

    def get_model_description(self, model_admin, request) -> str | None:
        description = getattr(model_admin, "mcp_description", None)
        if callable(description):
            description = description(request)
        return str(description) if description else None

    def is_view_exposed(self, model_admin, spec) -> bool:
        """Is the custom view ``spec`` (``@admin_api.view``) of ``model_admin`` exposed through MCP?
        Called after the view's own ``api_enabled`` / ``mcp_enabled`` and the read-only rule."""
        return True

    def get_tools(self, request=None) -> list[ToolSpec]:
        """The generic tools plus the views published with ``mcp_tool=``. With a ``request``, a
        view tool is only listed when the user may run the view (as checked without an object)."""
        from .handlers import TOOLS

        tools = [tool for tool in TOOLS if not (self.read_only and tool.writes)]
        return tools + self.get_view_tools(request, reserved={t.name for t in tools})

    def get_view_tools(self, request=None, *, reserved=()) -> list[ToolSpec]:
        from .. import custom_views
        from .handlers import view_tool

        out, seen = [], {}
        for model, model_admin in self.api_site.exposed_admins():
            if not self.is_exposed(model_admin):
                continue
            label = f"{model._meta.app_label}.{model._meta.model_name}"
            for spec in custom_views.declared_views(model_admin).values():
                if not spec.mcp_tool:
                    continue
                if spec.mcp_tool in reserved or spec.mcp_tool in seen:
                    other = seen.get(spec.mcp_tool, "a built-in tool")
                    raise ImproperlyConfigured(
                        f"MCP tool name {spec.mcp_tool!r} of view {label}:{spec.name} is already used by {other}."
                    )
                seen[spec.mcp_tool] = f"{label}:{spec.name}"
                if not spec.api_enabled or not spec.mcp_enabled or (self.read_only and spec.writes):
                    continue
                if not self.is_view_exposed(model_admin, spec):
                    continue
                if request is not None and not (
                    model_admin.has_view_or_change_permission(request)
                    and custom_views.has_view_permission(model_admin, request, spec)
                ):
                    continue
                out.append(view_tool(self, model_admin, spec))
        return out

    def get_tool(self, name: str, request=None) -> ToolSpec | None:
        return next((tool for tool in self.get_tools(request) if tool.name == name), None)

    def authenticate(self, request):
        """Set ``request.user`` from the first authenticator that recognizes the request."""
        for authenticator_class in self.authenticators:
            user = authenticator_class().authenticate(request)
            if user is not None:
                request.user = user
                return user
        raise NotAuthenticated("Send 'Authorization: Bearer <api token>'.")

    # --- calling tools -------------------------------------------------------------------------
    @staticmethod
    def prepare_request(request):
        """Mark ``request`` as an MCP request (also an API request, so api_ twins apply)."""
        request.is_api = True
        request.is_mcp = True
        request.is_dry_run = False
        return request

    def call_tool(self, request, name: str, arguments: dict | None) -> ToolResult:
        """Run a tool for ``request.user``. Errors become ``is_error`` results with the REST envelope."""
        self.prepare_request(request)
        tool = self.get_tool(name, request)
        if tool is None:
            return ToolResult(_error(ApiError(f"Unknown tool {name!r}.", code="unknown_tool")), is_error=True)
        if arguments is not None and not isinstance(arguments, dict):
            return ToolResult(_error(ApiError("Arguments must be an object.", code="invalid_arguments")), True)
        if not self.api_site.has_permission(request):
            return ToolResult(_error(PermissionDeniedError()), is_error=True)
        try:
            return ToolResult(tool.handler(self, request, arguments or {}))
        except ApiError as exc:
            return ToolResult(_error(exc), is_error=True)
        except Exception:
            logger.exception("MCP tool %s failed", name)
            return ToolResult({"error": "server_error", "detail": "The tool failed; see the server log."}, True)

    # --- transports (need the [mcp] extra) -------------------------------------------------------
    def get_urls(self):
        from .views import mcp_urlpatterns

        return mcp_urlpatterns(self)

    @property
    def urls(self):
        return self.get_urls(), "admin_mcp", self.name


def _error(exc: ApiError) -> dict:
    payload = {"error": exc.code, "status": exc.status}
    if exc.detail:
        payload["detail"] = exc.detail
    payload.update(exc.extra)
    return payload
