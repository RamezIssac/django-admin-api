from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.html import format_html

import admin_api

from .models import Client, Sales, SalesItem, Secret, Tag

site = admin.AdminSite(name="testadmin")

# Side-effect probes for the MCP dry_run tests
SAVE_LOG: list = []  # (slug, request.is_dry_run) per save_model call
ON_COMMIT_LOG: list = []  # slugs whose on_commit callback ran


class StatementForm(forms.Form):
    date_from = forms.DateField(required=False)
    date_to = forms.DateField(required=False)
    status = forms.ChoiceField(choices=Sales.STATUS, required=False)


@admin.register(Client, site=site)
class ClientAdmin(admin_api.ApiAdminMixin, admin.ModelAdmin):
    mcp_description = "Customers. Sales point to a client."
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

    # --- custom views (@admin_api.view) -------------------------------------------------------
    @admin_api.view(detail=True, params=StatementForm, mcp_tool="client_statement")
    def statement(self, request, obj, date_from=None, date_to=None, status=""):
        """Sales of one client between two dates, with the total."""
        qs = obj.sales_set.order_by("date", "slug")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if status:
            qs = qs.filter(status=status)
        return {
            "client": obj,
            "from": date_from,
            "lines": [{"slug": s.slug, "date": s.date, "value": s.value} for s in qs],
            "total": sum((s.value for s in qs), Decimal(0)),
            "mcp": request.is_mcp,
        }

    @admin_api.view(
        params={
            "prefix": {"type": "string", "description": "Name prefix."},
            "limit": {"type": "integer", "minimum": 1, "default": 10},
            "since": {"type": ["string", "null"], "format": "date"},
            "flags": {"type": "array", "items": {"type": "integer"}},
            "sort": {"enum": ["name", "-name"]},
        },
        required=["prefix"],
        description="Client names starting with a prefix.",
    )
    def names(self, request, prefix, limit, since=None, flags=None, sort="name"):
        qs = self.get_queryset(request).filter(name__startswith=prefix).order_by(sort)
        return {"names": [c.name for c in qs[:limit]], "since": since, "flags": flags}

    @admin_api.view(mcp_enabled=False)
    def rest_only(self, request):
        return "rest"

    @admin_api.view(api_enabled=False)
    def hidden(self, request):
        return "hidden"


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
    mcp_enabled = False  # in the REST API, hidden from MCP


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

    # --- custom views (@admin_api.view): a plain admin, no mixin --------------------------------
    @admin_api.view(
        detail=True,
        permissions=["change"],
        params={"note": {"type": "string", "default": ""}},
        description="Post a draft sale.",
        mcp_tool="post_sale",
    )
    def post(self, request, obj, note: str = ""):
        if obj.status == "posted":
            raise ValidationError("Already posted.")
        obj.status = "posted"
        obj.save(update_fields=["status"])
        SAVE_LOG.append((obj.slug, getattr(request, "is_dry_run", None)))
        self.message_user(request, f"{obj.slug} posted.")
        return {"status": obj.status, "note": note}

    @admin_api.view(permissions=["publish"], params={"note": {"type": "string"}})
    def publish_all(self, request, note=None):
        updated = self.get_queryset(request).update(status="posted")
        return {"updated": updated}

    def has_publish_permission(self, request):
        return request.user.is_superuser

    @admin_api.view(writes=True)
    def as_page(self, request):
        from django.http import HttpResponse

        self.get_queryset(request).update(status="posted")  # must be rolled back
        return HttpResponse("<html>")

    @admin_api.view
    def forbidden(self, request):
        raise PermissionDenied("not today")

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
        SAVE_LOG.append((obj.slug, getattr(request, "is_dry_run", None)))
        transaction.on_commit(lambda: ON_COMMIT_LOG.append(obj.slug))
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
