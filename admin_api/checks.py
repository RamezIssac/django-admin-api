from django.conf import settings
from django.core import checks

from .resolver import MIXIN_REQUIRED, defined_twins

MIDDLEWARE_PATH = "admin_api.middleware.ApiRequestMiddleware"


@checks.register()
def check_middleware(app_configs=None, **kwargs):
    if MIDDLEWARE_PATH not in getattr(settings, "MIDDLEWARE", []):
        return [
            checks.Error(
                f"'{MIDDLEWARE_PATH}' must be in MIDDLEWARE.",
                hint="It sets request.is_api = False on every request so admin code can branch on it.",
                id="admin_api.E001",
            )
        ]
    return []


@checks.register()
def check_twins_need_mixin(app_configs=None, **kwargs):
    from .mixins import ApiAdminMixin
    from .sites import all_api_sites

    errors = []
    seen = set()
    for api_site in all_api_sites():
        for model_admin in api_site.admin_site._registry.values():
            if id(model_admin) in seen or isinstance(model_admin, ApiAdminMixin):
                continue
            seen.add(id(model_admin))
            needs = sorted(defined_twins(model_admin) & MIXIN_REQUIRED)
            if needs:
                errors.append(
                    checks.Warning(
                        f"{type(model_admin).__name__} defines api_ twins for {', '.join(needs)} "
                        "but does not use ApiAdminMixin; they will be ignored inside Django's form machinery.",
                        hint="Add admin_api.ApiAdminMixin to the admin's bases.",
                        obj=type(model_admin),
                        id="admin_api.W001",
                    )
                )
    return errors
