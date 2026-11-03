from django.contrib import admin
from django.utils.html import format_html

import admin_api

from .models import Client, Sales, Secret

site = admin.AdminSite(name="testadmin")


@admin.register(Client, site=site)
class ClientAdmin(admin_api.ApiAdminMixin, admin.ModelAdmin):
    list_display = ["name", "badge"]
    api_list_display = ["name", "notes_length"]
    readonly_fields = []
    api_readonly_fields = ["notes"]
    search_fields = ["name"]

    @admin.display(description="Badge")
    def badge(self, obj):
        return format_html("<span class='badge'>{}</span>", obj.name)

    @admin_api.display(description="Notes length")
    def notes_length(self, obj):
        return len(obj.notes)

    def get_api_queryset(self, request):
        # Calling super() must reach the *normal* queryset, not recurse.
        return super().get_queryset(request).filter(name__startswith="A")


@admin.register(Sales, site=site)
class SalesAdmin(admin.ModelAdmin):  # plain admin, no mixin
    list_display = ["slug", "client", "value"]

    def get_api_list_display(self, request):
        return ["slug", "value"]


@admin.register(Secret, site=site)
class SecretAdmin(admin.ModelAdmin):
    api_enabled = False
