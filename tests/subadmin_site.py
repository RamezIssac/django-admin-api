"""An admin site using django-subadmin: Client → Sales → SalesItem. Imported only when
django-subadmin is installed (see tests/test_subadmin.py)."""

from django.contrib import admin
from subadmin import RootSubAdmin, SubAdmin

from admin_api import AdminApiSite
from admin_api.mcp import AdminMcpSite

from .testapp.models import Client, Sales, SalesItem


class SalesItemSub(SubAdmin):
    model = SalesItem
    list_display = ["product", "quantity", "price"]


class SalesSub(SubAdmin):
    model = Sales
    mcp_description = "Sales of one client."
    list_display = ["slug", "status", "value"]
    list_filter = ["status"]
    search_fields = ["slug"]
    fields = ["slug", "date", "status", "value"]
    subadmins = [SalesItemSub]


class ClientRoot(RootSubAdmin):
    list_display = ["name"]
    subadmins = [SalesSub]


site = admin.AdminSite(name="subadmin_test")
site.register(Client, ClientRoot)
site.register(Sales)  # also top-level: a different admin with a different path

api_site = AdminApiSite(admin_site=site, name="subadmin_api", register_token_admin=False)
mcp_site = AdminMcpSite(api_site, name="subadmin_mcp")
