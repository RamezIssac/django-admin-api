from tests.settings import *

INSTALLED_APPS = INSTALLED_APPS + ["admin_api"]
MIDDLEWARE = MIDDLEWARE + ["admin_api.middleware.ApiRequestMiddleware"]
ROOT_URLCONF = "ra_smoke_urls"
USE_TZ = True
