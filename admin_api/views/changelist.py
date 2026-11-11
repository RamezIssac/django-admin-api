"""REST adapters over :mod:`admin_api.services`: parse the HTTP request, call the service,
wrap the result in a ``JsonResponse``."""

from __future__ import annotations

from django.http import JsonResponse
from django.views import View

from .. import services
from ..writes import parse_json_body


class ModelApiView(View):
    api_site = None

    def get_model_admin(self, request, app_label, model_name):
        return services.get_model_admin(self.api_site, request, app_label, model_name)


class ChangeListView(ModelApiView):
    """``GET /v1/<app>/<model>/`` (Django's changelist as JSON), ``POST`` (create)."""

    def get(self, request, app_label, model_name):
        model_admin = self.get_model_admin(request, app_label, model_name)
        return JsonResponse(services.changelist(self.api_site, model_admin, request))

    def post(self, request, app_label, model_name):
        model_admin = self.get_model_admin(request, app_label, model_name)
        return JsonResponse(services.create(model_admin, request, lambda: parse_json_body(request)), status=201)


class ActionView(ModelApiView):
    """``POST /v1/<app>/<model>/actions/<name>/``"""

    def post(self, request, app_label, model_name, action_name):
        model_admin = self.get_model_admin(request, app_label, model_name)
        return JsonResponse(services.run_action(model_admin, request, action_name, parse_json_body(request)))


class SchemaView(ModelApiView):
    """``GET /v1/<app>/<model>/schema/``: what the list endpoint accepts and returns."""

    def get(self, request, app_label, model_name):
        model_admin = self.get_model_admin(request, app_label, model_name)
        return JsonResponse(services.schema(self.api_site, model_admin, request))
