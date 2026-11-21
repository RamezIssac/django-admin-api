from django.apps import AppConfig
from django.db.models.signals import post_migrate, post_save


class SearchConfig(AppConfig):
    name = "admin_api.contrib.search"
    label = "admin_api_search"
    verbose_name = "Admin API search"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
        from .indexer import on_post_migrate, on_post_save

        post_save.connect(on_post_save, dispatch_uid="admin_api_search_post_save")
        post_migrate.connect(on_post_migrate, sender=self, dispatch_uid="admin_api_search_post_migrate")
