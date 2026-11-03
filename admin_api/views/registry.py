from __future__ import annotations

from django.apps import apps
from django.http import JsonResponse
from django.urls import reverse
from django.views import View


class RegistryView(View):
    """``GET /v1/`` — the apps/models the current user may use, with their permissions."""

    api_site = None

    def get(self, request):
        base = reverse(f"{self.api_site.name}:index")
        app_dict: dict[str, dict] = {}
        for model, model_admin in self.api_site.exposed_admins():
            if not model_admin.has_module_permission(request):
                continue
            perms = model_admin.get_model_perms(request)
            if not any(perms.values()):
                continue
            opts = model._meta
            app = app_dict.setdefault(
                opts.app_label,
                {
                    "app_label": opts.app_label,
                    "name": str(apps.get_app_config(opts.app_label).verbose_name),
                    "models": [],
                },
            )
            app["models"].append(
                {
                    "model": opts.model_name,
                    "object_name": opts.object_name,
                    "name": str(opts.verbose_name),
                    "name_plural": str(opts.verbose_name_plural),
                    "perms": perms,
                    "url": f"{base}{opts.app_label}/{opts.model_name}/",
                }
            )
        app_list = sorted(app_dict.values(), key=lambda a: a["name"].lower())
        for app in app_list:
            app["models"].sort(key=lambda m: m["name"].lower())
        return JsonResponse({"version": self.api_site.version, "apps": app_list})
