"""admin_api — make Django ModelAdmin API-ready through ``api_`` twins."""

from .custom_views import view
from .decorators import display
from .mixins import ApiAdminMixin
from .resolver import api_value, resolve
from .sites import AdminApiSite

__all__ = ["AdminApiSite", "ApiAdminMixin", "api_value", "display", "resolve", "view"]
