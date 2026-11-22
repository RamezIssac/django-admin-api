"""A document admin with file and image fields, and file attachments as an inline."""

from django.contrib import admin

from admin_api import AdminApiSite
from admin_api.mcp import AdminMcpSite

from .models import Attachment, Document

files_site = admin.AdminSite(name="filesadmin")


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0


class DocumentAdmin(admin.ModelAdmin):
    inlines = [AttachmentInline]


files_site.register(Document, DocumentAdmin)
files_api_site = AdminApiSite(admin_site=files_site, name="files_api")
files_mcp = AdminMcpSite(files_api_site, name="files_mcp")
