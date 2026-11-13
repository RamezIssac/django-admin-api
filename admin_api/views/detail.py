from __future__ import annotations

from django.http import JsonResponse

from .. import services
from ..writes import parse_json_body
from .changelist import ModelApiView


class ObjectView(ModelApiView):
    """``/v1/<app>/<model>/<pk>/``: GET (detail), PATCH/PUT (update), DELETE."""

    def get(self, request, app_label, model_name, object_id):
        model_admin = self.get_model_admin(request, app_label, model_name)
        return JsonResponse(services.detail(model_admin, request, object_id))

    def patch(self, request, app_label, model_name, object_id):
        return self._update(request, app_label, model_name, object_id, partial=True)

    def put(self, request, app_label, model_name, object_id):
        return self._update(request, app_label, model_name, object_id, partial=False)

    def _update(self, request, app_label, model_name, object_id, *, partial):
        model_admin = self.get_model_admin(request, app_label, model_name)
        # the body is parsed lazily, after the permission check
        payload = services.update(model_admin, request, object_id, lambda: parse_json_body(request), partial=partial)
        return JsonResponse(payload)

    def delete(self, request, app_label, model_name, object_id):
        model_admin = self.get_model_admin(request, app_label, model_name)
        return JsonResponse(services.delete(model_admin, request, object_id))


class DeletePreviewView(ModelApiView):
    """``GET /v1/<app>/<model>/<pk>/delete-preview/``: what a DELETE would remove."""

    def get(self, request, app_label, model_name, object_id):
        model_admin = self.get_model_admin(request, app_label, model_name)
        return JsonResponse(services.delete_preview(model_admin, request, object_id))
