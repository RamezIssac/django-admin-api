from __future__ import annotations

from django.http import JsonResponse

from ..deletion import delete_object, delete_preview
from ..detail import serialize_object
from ..errors import PermissionDeniedError
from .changelist import ModelApiView


class ObjectView(ModelApiView):
    """``/v1/<app>/<model>/<pk>/`` — GET (detail), DELETE; PATCH/PUT added by the write side."""

    def get(self, request, app_label, model_name, object_id):
        model_admin = self.get_model_admin(request, app_label, model_name)
        obj = self.get_object(model_admin, request, object_id)
        if not model_admin.has_view_or_change_permission(request, obj):
            raise PermissionDeniedError()
        return JsonResponse(serialize_object(model_admin, request, obj))

    def delete(self, request, app_label, model_name, object_id):
        model_admin = self.get_model_admin(request, app_label, model_name)
        obj = self.get_object(model_admin, request, object_id)
        if not model_admin.has_delete_permission(request, obj):
            raise PermissionDeniedError()
        return JsonResponse({"deleted": delete_object(model_admin, request, obj)})


class DeletePreviewView(ModelApiView):
    """``GET /v1/<app>/<model>/<pk>/delete-preview/`` — what a DELETE would remove."""

    def get(self, request, app_label, model_name, object_id):
        model_admin = self.get_model_admin(request, app_label, model_name)
        obj = self.get_object(model_admin, request, object_id)
        if not model_admin.has_delete_permission(request, obj):
            raise PermissionDeniedError()
        return JsonResponse(delete_preview(model_admin, request, obj))
