from __future__ import annotations

import contextlib
import logging
from weakref import WeakSet

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import translation
from django.utils.translation import gettext

from ..auth import TokenAuthenticator
from ..errors import ApiError, NotAuthenticated, PermissionDeniedError
from ..memory import DEVELOPER, load_user_memory
from ..mixins import ApiAdminMixin
from .resolve import LiveEntityBackend
from .tools import ToolResult, ToolSpec

logger = logging.getLogger("admin_api.mcp")
audit_logger = logging.getLogger("admin_api.mcp.audit")

_all_sites: WeakSet = WeakSet()


def mcp_enabled() -> bool:
    return getattr(settings, "ADMIN_API_MCP_ENABLED", True)


def all_mcp_sites():
    return list(_all_sites)


BUSINESS_WORDING = """\
Talk to the user in business language: name a kind of record by its list_models 'name' or 'name_plural' \
(e.g. "client"), not its 'model' path; name a record or related record by its 'label'. \
Say "ID" only when the user asks or two records share a label. If a kind of record is unavailable, say so \
plainly, without model paths, permission codenames or field names.
"""
DEVELOPER_WORDING = """\
The user is a developer: you may name model paths (app_label.model_name), field names and IDs.
"""
DEFAULT_INSTRUCTIONS = (
    """\
This server gives you the user's business records and documents. You act as the user, with exactly their \
permissions; validation and business rules are the admin's own.
"""
    + BUSINESS_WORDING
    + """\
1. list_models shows what you can access.
2. Call describe_model before filtering or writing: filters, sortable columns, actions, create/update \
JSON Schema.
3. Prefer dry_run=true before create_object/update_object; results include the document's 'totals' when \
declared. If a dry run is required, a real write needs the confirm_token of a valid dry run of the same \
write: one per write, valid while the object is unchanged.
4. delete_object deletes only with confirm=true; otherwise it previews.
5. Custom views (reports, statements, business operations) are under 'views' in list_models, parameters \
in describe_model; run them with run_view or their own tool.
6. Resolve names the user typed with resolve_entities before you filter, report or write. Note: pass those \
IDs as describe_model's filter for that kind of record (e.g. 'client'), not as 'search' text, because \
search does not look into related records; an empty search returns 'searched_fields' and 'id_filters'. A \
word naming a kind of record ('invoices') resolves under 'models': use it with list_objects or its views. \
'unique': for a read, use it and name its label; for a write, only when match is 'exact' or 'exact_code', \
else ask. 'ambiguous': ask which one before running anything (not a run per candidate), \
listing each candidate's label and the kind of record. 'none': say nothing matched and offer the unfiltered answer, not a guessed filter or field.
Errors use the REST envelope (error, detail, fields, non_field_errors, inlines)."""
)


class AdminMcpSite:
    """Exposes an :class:`admin_api.AdminApiSite` as an MCP server.

    Same admins, permissions, token scopes and write pipeline as the REST API. Hide an admin
    from MCP only with ``mcp_enabled = False``; give the LLM context with ``mcp_description``.

    The transports are ``urls`` (Streamable HTTP) and ``manage.py mcp_serve`` (stdio); both are
    off when ``settings.ADMIN_API_MCP_ENABLED`` is False.
    """

    default_per_page = 25
    confirm_token_max_age = 900  # seconds a dry run's confirm_token stays valid
    list_max_rows = 1000  # list_objects pages through this many rows of a query; None: no cap
    instructions = DEFAULT_INSTRUCTIONS
    # app labels or "app_label.model_name" that resolve_entities never searches
    excluded_models = ("auth", "sessions", "contenttypes", "admin", "admin_api")
    entity_backend_class = LiveEntityBackend
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
        require_dry_run: bool = False,
        excluded_models=None,
    ):
        self.api_site = api_site
        self.name = name
        self.read_only = read_only
        self.server_name = server_name or name
        self.require_dry_run = require_dry_run
        if instructions is not None:
            self.instructions = instructions
        if excluded_models is not None:
            self.excluded_models = tuple(excluded_models)
        _all_sites.add(self)

    # --- policy hooks (override in a subclass) -------------------------------------------------
    def is_exposed(self, model_admin) -> bool:
        return self.api_site.is_exposed(model_admin) and getattr(model_admin, "mcp_enabled", True) is not False

    def get_model_description(self, model_admin, request) -> str | None:
        """Calls the admin's ``get_mcp_description(request)`` hook; admins without the mixin
        get its default."""
        hook = getattr(model_admin, "get_mcp_description", None)
        if hook is None:
            return ApiAdminMixin.get_mcp_description(model_admin, request)
        description = hook(request)
        return str(description) if description else None

    def is_view_exposed(self, model_admin, spec) -> bool:
        """Is the custom view ``spec`` (``@admin_api.view``) of ``model_admin`` exposed through MCP?
        Called after the view's own ``api_enabled`` / ``mcp_enabled`` and the read-only rule."""
        return True

    def requires_dry_run(self, model_admin) -> bool:
        """Must writes to ``model_admin`` follow a valid dry run? The admin's ``mcp_require_dry_run``
        (``True``/``False``) overrides the site's ``require_dry_run``."""
        override = getattr(model_admin, "mcp_require_dry_run", None)
        return self.require_dry_run if override is None else bool(override)

    def get_entity_backend(self):
        """The backend ``resolve_entities`` finds candidates with (default: each admin's own search)."""
        return self.entity_backend_class(self)

    def get_user_memory(self, request):
        """The user's :class:`admin_api.memory.UserMemory`, loaded once and cached on ``request.mcp_memory``."""
        memory = getattr(request, "mcp_memory", None)
        if memory is None:
            memory = request.mcp_memory = load_user_memory(getattr(request, "user", None))
        return memory

    def get_instructions(self, request) -> str:
        """The server instructions sent to ``request``'s client at ``initialize``: ``instructions``
        adjusted to the user's audience, language and notes (see :meth:`get_user_memory`)."""
        memory = self.get_user_memory(request)
        text = self.instructions
        if memory.audience == DEVELOPER:
            text = text.replace(BUSINESS_WORDING, DEVELOPER_WORDING)
        lines = [text]
        if memory.language:
            name = translation.get_language_info(memory.language)["name"]
            lines.append(f"Answer the user in {name}.")
        if memory.notes:
            lines.append(f"The user's own notes for you: {memory.notes}")
        return "\n".join(lines)

    def get_language(self, request) -> str | None:
        """The language tool calls and tool lists run in: the user's ``language`` preference, else
        ``None`` (keep the active one, e.g. from ``LocaleMiddleware``)."""
        return self.get_user_memory(request).language

    def translation_override(self, request):
        language = self.get_language(request) if request is not None else None
        return translation.override(language) if language else contextlib.nullcontext()

    @property
    def confirm_salt(self) -> str:
        return f"admin_api.mcp.confirm.{self.name}"

    def get_tools(self, request=None) -> list[ToolSpec]:
        """The generic tools plus the views published with ``mcp_tool=``. With a ``request``, a
        view tool is only listed when the user may run the view (as checked without an object)."""
        from .handlers import TOOLS

        tools = [tool for tool in TOOLS if not (self.read_only and tool.writes)]
        with self.translation_override(request):
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
    def prepare_request(self, request):
        """Mark ``request`` as an MCP request (also an API request, so api_ twins apply) and load
        the user's memory into ``request.mcp_memory``."""
        request.is_api = True
        request.is_mcp = True
        request.is_dry_run = False
        self.get_user_memory(request)
        return request

    def call_tool(self, request, name: str, arguments: dict | None) -> ToolResult:
        """Run a tool for ``request.user``. Errors become ``is_error`` results with the REST envelope.
        Every call is logged to ``admin_api.mcp.audit`` (see :func:`audit`)."""
        try:
            with self.translation_override(request):
                result = self._call_tool(request, name, arguments)
        except Exception:
            audit(request, name, arguments, ToolResult({"error": "server_error"}, is_error=True))
            raise
        audit(request, name, arguments, result)
        return result

    def _call_tool(self, request, name: str, arguments: dict | None) -> ToolResult:
        self.prepare_request(request)
        tool = self.get_tool(name, request)
        if tool is None:
            return ToolResult(_error(ApiError(f"Unknown tool {name!r}.", code="unknown_tool")), is_error=True)
        if arguments is not None and not isinstance(arguments, dict):
            return ToolResult(_error(ApiError("Arguments must be an object.", code="invalid_arguments")), True)
        if not self.api_site.has_permission(request):
            return ToolResult(_error(PermissionDeniedError(gettext("You may not use this server."))), is_error=True)
        try:
            return ToolResult(tool.handler(self, request, arguments or {}))
        except ApiError as exc:
            return ToolResult(_error(exc), is_error=True)
        except Exception:
            logger.exception("MCP tool %s failed", name)
            return ToolResult({"error": "server_error", "detail": "The tool failed; see the server log."}, True)

    # --- transports ------------------------------------------------------------------------------
    def get_urls(self):
        from .views import mcp_urlpatterns

        return mcp_urlpatterns(self)

    @property
    def urls(self):
        return self.get_urls(), "admin_mcp", self.name


def audit(request, name: str, arguments, result: ToolResult) -> None:
    """One INFO record per tool call: who, from where, which tool and target, and the outcome.
    ``data``, ``params`` and ``confirm_token`` are not logged; caller strings are logged with repr."""
    args = arguments if isinstance(arguments, dict) else {}
    token = getattr(request, "api_token", None)
    record = {
        "tool": name,
        "user": getattr(getattr(request, "user", None), "pk", None),
        "token": getattr(token, "prefix", None),
        "ip": request.META.get("REMOTE_ADDR"),
        "model": args.get("model"),
        "pk": args.get("id", args.get("pk")),
        "view": args.get("view"),
        "action": args.get("action"),
        "pks": args.get("ids", args.get("pks")),
        "all": args.get("all") is True,
        "dry_run": args.get("dry_run") is True,
        "confirm": args.get("confirm") is True,
        "outcome": result.data.get("error", "error") if result.is_error else "ok",
    }
    audit_logger.info(
        "MCP %(tool)r user=%(user)s token=%(token)r ip=%(ip)r model=%(model)r pk=%(pk)r view=%(view)r "
        "action=%(action)r pks=%(pks)r all=%(all)s dry_run=%(dry_run)s confirm=%(confirm)s outcome=%(outcome)r",
        record,
        extra={"mcp_audit": record},
    )


def _error(exc: ApiError) -> dict:
    payload = {"error": exc.code, "status": exc.status}
    if exc.detail:
        payload["detail"] = exc.detail
    payload.update(exc.extra)
    return payload
