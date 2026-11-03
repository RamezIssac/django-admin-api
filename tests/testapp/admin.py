from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html

import admin_api

from .models import Client, Sales, SalesItem, Secret, Tag

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


class SalesItemInline(admin.TabularInline):
    model = SalesItem
    extra = 0


@admin.register(Tag, site=site)
class TagAdmin(admin.ModelAdmin):
    pass


class SalesForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        if cleaned.get("slug") and not cleaned["slug"].startswith(("S-", "HIDDEN")):
            raise forms.ValidationError("Slug must start with S-.")
        return cleaned


@admin.register(Sales, site=site)
class SalesAdmin(admin.ModelAdmin):  # plain admin, no mixin
    form = SalesForm
    inlines = [SalesItemInline]
    readonly_fields = ["badge", "doubled"]
    list_display = ["slug", "date", "client", "status", "value", "badge", "doubled"]
    list_filter = ["status", "client", ValueBandFilter]
    search_fields = ["slug", "client__name"]
    api_search_fields = ["slug"]
    date_hierarchy = "date"
    ordering = ["slug"]
    api_list_per_page = 2
    actions = ["mark_posted", "export_csv", "boom"]

    @admin.action(description="Mark as posted", permissions=["change"])
    def mark_posted(self, request, queryset):
        updated = queryset.update(status="posted")
        self.message_user(request, f"{updated} posted.")

    @admin.action(description="Export CSV")
    def export_csv(self, request, queryset):
        from django.http import HttpResponse

        queryset.update(status="draft")  # side effect that must be rolled back
        return HttpResponse("slug\n", content_type="text/csv")

    def api_export_csv(self, request, queryset):
        return {"rows": [{"slug": s.slug} for s in queryset.order_by("slug")]}

    @admin.action(description="Not allowed")
    def boom(self, request, queryset):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("nope")

    @admin.display(description="Badge")  # HTML, not marked -> hidden from the API
    def badge(self, obj):
        return format_html("<b>{}</b>", obj.slug)

    @admin_api.display(description="Doubled", ordering="value")
    def doubled(self, obj):
        return obj.value * 2

    def get_api_queryset(self, request):
        return super().get_queryset(request).exclude(slug="HIDDEN")

    def save_model(self, request, obj, form, change):
        obj.saved_via_api = request.is_api
        super().save_model(request, obj, form, change)
        messages.info(request, f"Saved {obj.slug}")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        sales = form.instance
        total = sum(i.quantity * i.price for i in sales.items.all())
        if sales.items.exists() and total != sales.value:
            sales.value = total
            sales.save(update_fields=["value"])


@admin.register(Secret, site=site)
class SecretAdmin(admin.ModelAdmin):
    api_enabled = False
