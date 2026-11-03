# admin_api

A thin layer that makes Django `ModelAdmin` API-ready, and later MCP-ready.

The same `ModelAdmin` drives the HTML admin and the JSON API. Where the API should behave
differently, write an `api_` twin:

```python
class SalesAdmin(admin_api.ApiAdminMixin, admin.ModelAdmin):
    list_display = ["slug", "client", "get_stats_icon", "value"]
    api_list_display = ["slug", "client", "value"]  # attribute twin

    def get_api_queryset(self, request):  # method twin
        return super().get_queryset(request).select_related("client")
```

Lookup order: `get_api_X(request)` → `api_X` → `get_X(request)` → `X`. Anything else can
branch on `request.is_api`.

```python
# settings.py
INSTALLED_APPS += ["admin_api"]
MIDDLEWARE += ["admin_api.middleware.ApiRequestMiddleware"]

# urls.py
api_site = AdminApiSite(admin_site=admin.site)
urlpatterns += [path("api/", api_site.urls)]  # → /api/v1/
```

Status: prototype, milestone 1 of 8 (skeleton, resolver, session auth, registry endpoint).
Design: [docs/DESIGN.md](docs/DESIGN.md).

## Development

```bash
uv sync
uv run pytest
```
