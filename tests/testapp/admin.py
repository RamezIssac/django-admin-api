from django.contrib import admin
from django.utils.html import format_html

import admin_api

from .models import Client, Sales, Secret

site = admin.AdminSite(name="testadmin")


@admin.register(Client, site=site)
class ClientAdmin(admin_api.ApiAdminMixin, admin.ModelAdmin):
    list_display = ["name", "badge"]
    api_list_display = ["name", "notes_length"]  # explicit twin: callables listed here are exposed
    readonly_fields = []
    api_readonly_fields = ["notes"]
    search_fields = ["name"]

    @admin.display(description="Badge")
    def badge(self, obj):
        return format_html("<span class='badge'>{}</span>", obj.name)

    def notes_length(self, obj):
        return len(obj.notes)

    def get_api_queryset(self, request):
        # Calling super() must reach the *normal* queryset, not recurse.
        return super().get_queryset(request).filter(name__startswith="A")


class ValueBandFilter(admin.SimpleListFilter):
    title = "value band"
    parameter_name = "band"

    def lookups(self, request, model_admin):
        return [("high", "High (>= 1000)"), ("low", "Low")]

    def queryset(self, request, queryset):
        if self.value() == "high":
            return queryset.filter(value__gte=1000)
        if self.value() == "low":
            return queryset.filter(value__lt=1000)
        return queryset


@admin.register(Sales, site=site)
class SalesAdmin(admin.ModelAdmin):  # plain admin, no mixin
    list_display = ["slug", "date", "client", "status", "value", "badge", "doubled"]
    list_filter = ["status", "client", ValueBandFilter]
    search_fields = ["slug", "client__name"]
    api_search_fields = ["slug"]
    date_hierarchy = "date"
    ordering = ["slug"]
    api_list_per_page = 2

    @admin.display(description="Badge")  # HTML, not marked -> hidden from the API
    def badge(self, obj):
        return format_html("<b>{}</b>", obj.slug)

    @admin_api.display(description="Doubled", ordering="value")
    def doubled(self, obj):
        return obj.value * 2

    def get_api_queryset(self, request):
        return super().get_queryset(request).exclude(slug="HIDDEN")


@admin.register(Secret, site=site)
class SecretAdmin(admin.ModelAdmin):
    api_enabled = False
