"""A sales admin whose header total is written by a signal, behind the saved instance's back."""

from decimal import Decimal

from django.contrib import admin

from admin_api import AdminApiSite
from admin_api.mcp import AdminMcpSite

from .models import Sales, SalesItem

totals_site = admin.AdminSite(name="totalsadmin")


class SalesItemInline(admin.TabularInline):
    model = SalesItem
    extra = 0


class TotalsSalesAdmin(admin.ModelAdmin):
    inlines = [SalesItemInline]
    api_totals = ["value", "line_count"]

    @admin.display(description="Lines")
    def line_count(self, obj):
        return obj.items.count()


totals_site.register(Sales, TotalsSalesAdmin)
totals_api_site = AdminApiSite(admin_site=totals_site)
totals_mcp = AdminMcpSite(totals_api_site, name="totals_mcp")


def sync_header_value(sender, instance, **kwargs):
    total = sum((i.quantity * i.price for i in SalesItem.objects.filter(sales_id=instance.sales_id)), Decimal(0))
    Sales.objects.filter(pk=instance.sales_id).update(value=total)
