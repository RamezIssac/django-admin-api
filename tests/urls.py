from django.urls import path

from admin_api import AdminApiSite
from tests.testapp.admin import site

api_site = AdminApiSite(admin_site=site)

urlpatterns = [
    path("admin/", site.urls),
    path("api/", api_site.urls),
]
