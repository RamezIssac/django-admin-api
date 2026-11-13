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


@checks.register(checks.Tags.urls)
def check_mcp_view_tools(app_configs=None, **kwargs):
    """Names of the views published as MCP tools (``mcp_tool=``) must be unique per MCP site."""
    from django.core.exceptions import ImproperlyConfigured
    from django.urls import get_resolver

    get_resolver().url_patterns  # noqa: B018  (import the URLconf, which creates the sites)
    from .mcp.sites import all_mcp_sites

    errors = []
    for site in all_mcp_sites():
        try:
            site.get_tools()
        except ImproperlyConfigured as exc:
            errors.append(checks.Error(str(exc), obj=site, id="admin_api.E002"))
    return errors
