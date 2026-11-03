from django.conf import settings
from django.core import checks

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
