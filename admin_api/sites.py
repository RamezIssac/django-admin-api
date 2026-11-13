from __future__ import annotations

from functools import wraps
from weakref import WeakSet

from django.contrib import admin
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from .auth import SessionAuthenticator, TokenAuthenticator
from .contrib.subadmin import SubAdminAdapter
from .errors import ApiError, MethodNotAllowed, NotAuthenticated, PermissionDeniedError
from .mixins import ApiAdminMixin

_all_sites: WeakSet = WeakSet()


def all_api_sites():
    return list(_all_sites)


class AdminApiSite:
    """Exposes the ModelAdmins of an existing ``AdminSite`` as a JSON API.

    No separate registration: every admin registered on ``admin_site`` is exposed unless
    ``is_exposed()`` says otherwise (default: ``model_admin.api_enabled`` is not ``False``).
    Subclass to change site-wide behaviour, like you would subclass ``AdminSite``.
    """

    authenticators = [TokenAuthenticator, SessionAuthenticator]
    version = "v1"
    max_page_size = 500
    # Nested admins (see admin_api.nested). Duck-typed: inactive unless the library is used.
    nested_adapters = [SubAdminAdapter]

    def __init__(
        self,
        admin_site: admin.AdminSite | None = None,
        *,
        name: str = "admin_api",
        version: str | None = None,
        register_token_admin: bool = True,
    ):
        self.admin_site = admin_site or admin.site
        self.name = name
        self._api_admins: dict = {}
        self._adapters = [adapter() for adapter in self.nested_adapters]
        if version is not None:
            self.version = version
        if register_token_admin:
            self.register_token_admin()
        _all_sites.add(self)

    def register_token_admin(self):
        """Register the API token admin on the wrapped admin site (once)."""
        from .admin import ApiTokenAdmin
        from .auth import get_token_model

        model = get_token_model()
        if not self.admin_site.is_registered(model):
            self.admin_site.register(model, ApiTokenAdmin)

    # --- policy hooks -----------------------------------------------------------------------
    def is_exposed(self, model_admin) -> bool:
        return getattr(model_admin, "api_enabled", True) is not False

    def has_permission(self, request) -> bool:
        return self.admin_site.has_permission(request)

    def authenticate(self, request):
        for authenticator_class in self.authenticators:
            user = authenticator_class().authenticate(request)
            if user is not None:
                request.user = user
                return user
        raise NotAuthenticated()

    def get_api_admin(self, model_admin):
        """The admin instance the API uses.

        Admins that already use ``ApiAdminMixin`` are used as-is. Others get a cached runtime
        subclass ``(ApiAdminMixin, TheirAdmin)`` so that Django's internal getter calls
        (ChangeList → get_queryset / get_search_results / get_ordering, get_form →
        get_fieldsets, ...) see the api_ twins too. The mixin is therefore optional.
        """
        if isinstance(model_admin, ApiAdminMixin):
            return model_admin
        adapter = self.get_nested_adapter(model_admin)
        # nested admins are cached per instance: the same class may be mounted under several parents
        key = model_admin.model if adapter is None else ("nested", id(model_admin))
        cached = self._api_admins.get(key)
        if cached is None or cached[0] is not model_admin:
            cls = type(model_admin)
            api_cls = type(f"Api{cls.__name__}", (ApiAdminMixin, cls), {"__module__": cls.__module__})
            if adapter is None:
                api_admin = api_cls(model_admin.model, model_admin.admin_site)
            else:
                api_admin = api_cls(*adapter.init_args(model_admin))
            cached = (model_admin, api_admin)
            self._api_admins[key] = cached
        return cached[1]

    def get_model_admin(self, app_label: str, model_name: str):
        for model, model_admin in self.admin_site._registry.items():
            opts = model._meta
            if opts.app_label == app_label and opts.model_name == model_name:
                if self.is_exposed(model_admin):
                    return self.get_api_admin(model_admin)
                break
        return None

    def exposed_admins(self):
        for model, model_admin in self.admin_site._registry.items():
            if self.is_exposed(model_admin):
                yield model, self.get_api_admin(model_admin)

    # --- nested admins (admin_api.nested) -----------------------------------------------------
    def get_nested_adapter(self, model_admin):
        """The adapter handling ``model_admin`` as a nested admin, or ``None`` for top-level admins."""
        return next((a for a in self._adapters if a.is_child(model_admin)), None)

    def get_api_children(self, model_admin) -> dict:
        """``{segment: api_admin}`` of the exposed admins nested directly under ``model_admin``."""
        children = {}
        for adapter in self._adapters:
            for child in adapter.get_children(model_admin):
                segment = adapter.segment(child)
                if segment not in children and self.is_exposed(child):
                    children[segment] = self.get_api_admin(child)
        return children

    def get_admin_chain(self, path) -> list | None:
        """API admins from the root to the target of ``path`` (a :class:`~admin_api.nested.ModelPath`),
        or ``None`` when any level is missing or not exposed (hiding an admin hides its children)."""
        root = self.get_model_admin(path.app_label, path.model_name)
        if root is None:
            return None
        chain = [root]
        for segment in path.segments:
            child = self.get_api_children(chain[-1]).get(segment)
            if child is None:
                return None
            chain.append(child)
        return chain

    def prepare_nested_request(self, model_admin, request, parents: list) -> None:
        """Let the nested ``model_admin``'s library load and check the ancestors for ``request``."""
        adapter = self.get_nested_adapter(model_admin)
        if adapter is not None:
            adapter.prepare_request(model_admin, request, parents)

    # --- view plumbing ----------------------------------------------------------------------
    def api_view(self, view, methods=("GET",)):
        """Wrap a view: mark request as API, check method, authenticate, check site permission,
        and turn ``ApiError`` into JSON. CSRF is enforced by the session authenticator only."""

        @csrf_exempt
        @wraps(view)
        def inner(request, *args, **kwargs):
            request.is_api = True
            try:
                if request.method not in methods:
                    raise MethodNotAllowed(f"Allowed: {', '.join(methods)}")
                self.authenticate(request)
                if not self.has_permission(request):
                    raise PermissionDeniedError()
                return view(request, *args, **kwargs)
            except ApiError as exc:
                return exc.as_response()

        return inner

    def get_urls(self):
        from .views.changelist import ActionView, ChangeListView, SchemaView
        from .views.detail import DeletePreviewView, ObjectView
        from .views.nested import NESTED_METHODS, nested_view
        from .views.registry import RegistryView

        v = self.version
        model = f"{v}/<str:app_label>/<str:model_name>/"
        return [
            path(f"{v}/", self.api_view(RegistryView.as_view(api_site=self)), name="index"),
            path(
                model,
                self.api_view(ChangeListView.as_view(api_site=self), methods=("GET", "POST")),
                name="changelist",
            ),
            path(f"{model}schema/", self.api_view(SchemaView.as_view(api_site=self)), name="schema"),
            path(
                f"{model}actions/<str:action_name>/",
                self.api_view(ActionView.as_view(api_site=self), methods=("POST",)),
                name="action",
            ),
            path(
                f"{model}<str:object_id>/",
                self.api_view(ObjectView.as_view(api_site=self), methods=("GET", "PATCH", "PUT", "DELETE")),
                name="object",
            ),
            path(
                f"{model}<str:object_id>/delete-preview/",
                self.api_view(DeletePreviewView.as_view(api_site=self)),
                name="delete_preview",
            ),
            # nested admins: <pk>/<child>/[<pk>/<child>/…] then the same tails as above
            path(
                f"{model}<path:nested>/",
                self.api_view(nested_view(self), methods=NESTED_METHODS),
                name="nested",
            ),
        ]

    @property
    def urls(self):
        return self.get_urls(), "admin_api", self.name
