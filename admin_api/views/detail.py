from __future__ import annotations

from django.http import JsonResponse

from ..deletion import delete_object, delete_preview
from ..detail import serialize_object
from ..errors import PermissionDeniedError
from ..writes import parse_json_body, save_from_payload
from .changelist import ModelApiView


def saved_response(model_admin, request, result, messages, status):
    payload = serialize_object(model_admin, request, result.obj)
    if messages:
        payload["messages"] = messages
    return JsonResponse(payload, status=status)


class ObjectView(ModelApiView):
    """``/v1/<app>/<model>/<pk>/`` — GET (detail), DELETE; PATCH/PUT added by the write side."""

    def get(self, request, app_label, model_name, object_id):
        model_admin = self.get_model_admin(request, app_label, model_name)
        obj = self.get_object(model_admin, request, object_id)
        if not model_admin.has_view_or_change_permission(request, obj):
            raise PermissionDeniedError()
        return JsonResponse(serialize_object(model_admin, request, obj))

    def patch(self, request, app_label, model_name, object_id):
        return self._update(request, app_label, model_name, object_id, partial=True)

    def put(self, request, app_label, model_name, object_id):
        return self._update(request, app_label, model_name, object_id, partial=False)

    def _update(self, request, app_label, model_name, object_id, *, partial):
        model_admin = self.get_model_admin(request, app_label, model_name)
        obj = self.get_object(model_admin, request, object_id)
        if not model_admin.has_change_permission(request, obj):
            raise PermissionDeniedError()
        payload = parse_json_body(request)
        result, messages = save_from_payload(model_admin, request, obj, payload, change=True, partial=partial)
        return saved_response(model_admin, request, result, messages, status=200)

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
