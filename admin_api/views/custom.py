from __future__ import annotations

from django.http import JsonResponse

from .. import custom_views
from ..errors import MethodNotAllowed
from ..writes import parse_json_body
from .changelist import ModelApiView


class CustomViewView(ModelApiView):
    """``/v1/<app>/<model>/views/<name>/`` and ``/v1/<app>/<model>/<pk>/views/<name>/``: a method
    decorated with ``@admin_api.view``. GET takes the parameters from the query string, POST from
    a JSON body; write views are POST only."""

    http_method_names = ["get", "post"]

    def get(self, request, app_label, model_name, view_name, object_id=None):
        return self._run(request, app_label, model_name, view_name, object_id, request.GET)

    def post(self, request, app_label, model_name, view_name, object_id=None):
        return self._run(request, app_label, model_name, view_name, object_id, None)

    def _run(self, request, app_label, model_name, view_name, object_id, data):
        model_admin = self.get_model_admin(request, app_label, model_name)
        spec = custom_views.get_view(model_admin, request, view_name)
        if request.method not in spec.methods:
            raise MethodNotAllowed(f"Allowed: {', '.join(spec.methods)}")
        if data is None:
            data = parse_json_body(request)
        return JsonResponse(custom_views.run_view(model_admin, request, view_name, object_id=object_id, data=data))
