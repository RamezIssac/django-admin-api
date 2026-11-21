"""The admin site and MCP site the search contrib tests share (fixtures: tests/conftest.py)."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from admin_api import AdminApiSite
from admin_api.mcp import AdminMcpSite
from tests.testapp import admin as testadmin
from tests.testapp.models import Client, Party, Payment, Sales, Tag


class PartyAdmin(admin.ModelAdmin):
    search_fields = ["^name", "code", "active", "joined"]
    search_aliases = {"en": ["suppliers"]}


site = admin.AdminSite(name="search_admin")
site.register(Client, testadmin.ClientAdmin)  # API queryset: names starting with "A"
site.register(Sales, testadmin.SalesAdmin)  # API search fields: slug
site.register(Tag, testadmin.TagAdmin)  # hidden from MCP
site.register(Party, PartyAdmin)
site.register(Payment, admin.ModelAdmin)
site.register(get_user_model(), UserAdmin)  # excluded by default
api_site = AdminApiSite(site, name="search_api")
mcp_site = AdminMcpSite(api_site, name="search_mcp")


def picks(listing, *keys):
    return [tuple(c[k] for k in keys) for c in listing["candidates"]]
