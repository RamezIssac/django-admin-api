from django.apps import AppConfig


class AdminApiConfig(AppConfig):
    name = "admin_api"
    verbose_name = "Admin API"

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
