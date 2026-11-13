from __future__ import annotations

from django.http import JsonResponse
from django.views import View

from .. import services


class RegistryView(View):
    """``GET /v1/``: the apps/models the current user may use, with their permissions."""

    api_site = None

    def get(self, request):
        return JsonResponse(services.registry(self.api_site, request))
