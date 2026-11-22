from django.urls import path

from admin_api import AdminApiSite
from admin_api.mcp import AdminMcpSite
from tests.testapp.admin import site
from tests.testapp.files_admin import files_api_site

api_site = AdminApiSite(admin_site=site)
mcp_site = AdminMcpSite(api_site, server_name="testapp")

urlpatterns = [
    path("admin/", site.urls),
    path("api/", api_site.urls),
    path("mcp/", mcp_site.urls),
    path("files-api/", files_api_site.urls),
]
