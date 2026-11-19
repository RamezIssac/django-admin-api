from django.apps import AppConfig


class AdminApiConfig(AppConfig):
    name = "admin_api"
    default_auto_field = "django.db.models.AutoField"  # matches migration 0001
    verbose_name = "Admin API"

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
