from __future__ import annotations

from . import scopes
from .resolver import is_resolving, resolve, resolving


def _is_api(request) -> bool:
    return bool(getattr(request, "is_api", False))


class ApiAdminMixin:
    """Routes Django's normal ``get_X`` getters to their ``api_`` twin when ``request.is_api``.

    Because Django's getters call each other (``get_form`` → ``get_fieldsets`` →
    ``get_fields``; ``get_readonly_fields`` decides exclusions), routing *inside* the getters
    makes every internal call API-aware.

    If you override one of these getters on your admin, call ``super()`` to keep the routing.
    """

    api_enabled = True

    # --- changelist-level ------------------------------------------------------------------
    def get_list_display(self, request):
        if _is_api(request) and not is_resolving(request, "list_display"):
            return resolve(self, "list_display", request)
        return super().get_list_display(request)

    def get_list_filter(self, request):
        if _is_api(request) and not is_resolving(request, "list_filter"):
            return resolve(self, "list_filter", request)
        return super().get_list_filter(request)

    def get_search_fields(self, request):
        if _is_api(request) and not is_resolving(request, "search_fields"):
            return resolve(self, "search_fields", request)
        return super().get_search_fields(request)

    def get_ordering(self, request):
        if _is_api(request) and not is_resolving(request, "ordering"):
            return resolve(self, "ordering", request)
        return super().get_ordering(request)

    def get_queryset(self, request):
        if _is_api(request) and not is_resolving(request, "queryset"):
            return resolve(self, "queryset", request)
        return super().get_queryset(request)

    # --- changeform-level ------------------------------------------------------------------
    def get_fields(self, request, obj=None):
        if _is_api(request) and not is_resolving(request, "fields"):
            return resolve(self, "fields", request, obj)
        return super().get_fields(request, obj)

    def get_fieldsets(self, request, obj=None):
        if _is_api(request) and not is_resolving(request, "fieldsets"):
            return resolve(self, "fieldsets", request, obj)
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if _is_api(request) and not is_resolving(request, "readonly_fields"):
            return resolve(self, "readonly_fields", request, obj)
        return super().get_readonly_fields(request, obj)

    def get_inlines(self, request, obj):
        if _is_api(request) and not is_resolving(request, "inlines"):
            inlines = resolve(self, "inlines", request, obj)
            if scopes.request_scopes(request) is not None:
                # inline formsets check the inline's own has_*_permission: scope those too
                inlines = [scopes.scoped_inline_class(inline) for inline in inlines]
            return inlines
        return super().get_inlines(request, obj)

    # --- permissions: token scopes narrow, never widen ---------------------------------------
    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and scopes.request_allows(request, self.opts, "view")

    def has_add_permission(self, request):
        return super().has_add_permission(request) and scopes.request_allows(request, self.opts, "add")

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and scopes.request_allows(request, self.opts, "change")

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and scopes.request_allows(request, self.opts, "delete")

    def get_actions(self, request, action_location=None):
        if _is_api(request) and not is_resolving(request, "actions"):
            method = getattr(self, "get_api_actions", None)
            if method is not None:
                with resolving(request, "actions"):
                    return method(request)
            api_actions = getattr(self, "api_actions", None)
            if api_actions is not None:
                base = [a for a in (self.get_action(name) for name in api_actions) if a]
                allowed = self._filter_actions_by_permissions(request, base)
                return {getattr(action, "name", None) or action[1]: action for action in allowed}
        if action_location is None:
            return super().get_actions(request)
        return super().get_actions(request, action_location=action_location)

    def get_form(self, request, obj=None, change=False, **kwargs):
        if _is_api(request) and not is_resolving(request, "form"):
            method = getattr(self, "get_api_form", None)
            if method is not None:
                with resolving(request, "form"):
                    return method(request, obj, change, **kwargs)
            api_form = getattr(self, "api_form", None)
            if api_form is not None and "form" not in kwargs:
                # api_form is a *base* form class, like ModelAdmin.form.
                kwargs["form"] = api_form
        return super().get_form(request, obj, change=change, **kwargs)
