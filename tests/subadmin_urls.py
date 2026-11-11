from django.urls import path

from tests.subadmin_site import api_site, site

urlpatterns = [
    path("admin/", site.urls),
    path("api/", api_site.urls),
]
