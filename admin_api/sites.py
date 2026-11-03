from __future__ import annotations

from functools import wraps
from weakref import WeakSet

from django.contrib import admin
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from .auth import SessionAuthenticator
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

    authenticators = [SessionAuthenticator]
    version = "v1"
    max_page_size = 500

    def __init__(
        self, admin_site: admin.AdminSite | None = None, *, name: str = "admin_api", version: str | None = None
    ):
        self.admin_site = admin_site or admin.site
        self.name = name
        self._api_admins: dict = {}
        if version is not None:
            self.version = version
        _all_sites.add(self)

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
        key = model_admin.model
        cached = self._api_admins.get(key)
        if cached is None or cached[0] is not model_admin:
            cls = type(model_admin)
            api_cls = type(f"Api{cls.__name__}", (ApiAdminMixin, cls), {"__module__": cls.__module__})
            cached = (model_admin, api_cls(model_admin.model, model_admin.admin_site))
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
        from .views.changelist import ChangeListView, SchemaView
        from .views.registry import RegistryView

        v = self.version
        model = f"{v}/<str:app_label>/<str:model_name>/"
        return [
            path(f"{v}/", self.api_view(RegistryView.as_view(api_site=self)), name="index"),
            path(model, self.api_view(ChangeListView.as_view(api_site=self)), name="changelist"),
            path(f"{model}schema/", self.api_view(SchemaView.as_view(api_site=self)), name="schema"),
        ]

    @property
    def urls(self):
        return self.get_urls(), "admin_api", self.name
