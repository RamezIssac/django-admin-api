"""django-subadmin support (https://github.com/inueni/django-subadmin).

A ``SubAdmin`` is not registered on the admin site: it hangs off a registered
``RootSubAdmin`` (``subadmins = [...]``), at any depth, and every one of its views needs
``request.subadmin`` (a ``SubAdminHelper`` that loads and checks the ancestors). This adapter
discovers the subadmins and builds that helper before admin_api calls a nested admin.

Duck-typed: nothing here imports ``subadmin``, so admin_api does not depend on it.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import Http404

from ..errors import NotFound, PermissionDeniedError
from ..nested import NestedAdapter


class SubAdminAdapter(NestedAdapter):
    def get_children(self, model_admin) -> list:
        children = getattr(model_admin, "subadmin_instances", None) or []
        return [child for child in children if self.is_child(child)]

    def is_child(self, model_admin) -> bool:
        return hasattr(model_admin, "get_subadmin_helper") and hasattr(model_admin, "parent_admin")

    def init_args(self, child) -> tuple:
        return (child.parent_model, child.parent_admin)

    def prepare_request(self, model_admin, request, parents: list) -> None:
        try:
            request.subadmin = model_admin.get_subadmin_helper(request, [str(pk) for pk in parents])
        except Http404 as exc:
            raise NotFound(f"Parent objects {list(parents)!r} not found (or not related to each other).") from exc
        except PermissionDenied as exc:
            raise PermissionDeniedError("No permission on a parent object.") from exc
