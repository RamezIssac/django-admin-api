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

Writes run the admin's own pipeline. The JSON is translated into the change form's POST data
and passed to `process_changeform(request, obj, change)` when the admin defines it (that's
how erp_framework's `whole_changeform_validation`/`pre_save`/`post_save` hooks run), or to a
default that mirrors `ModelAdmin._changeform_view`. LogEntry, reversion (`VersionAdmin`) and
`message_user` work as in the HTML admin.

```python
# settings.py
INSTALLED_APPS += ["admin_api"]
MIDDLEWARE += ["admin_api.middleware.ApiRequestMiddleware"]

# urls.py
api_site = AdminApiSite(admin_site=admin.site)
urlpatterns += [path("api/", api_site.urls)]  # → /api/v1/
```

Status: prototype, milestone 4 of 8.

| Endpoint | |
|---|---|
| `GET /api/v1/` | apps/models the user may use, with permissions |
| `GET /api/v1/<app>/<model>/` | changelist: admin filters (`?status__exact=`), search `?q=`, ordering `?o=`, page `?p=`, `_per_page`, `_shape=columnar` |
| `GET /api/v1/<app>/<model>/schema/` | columns, filters with their params, search, ordering, pagination |
| `POST /api/v1/<app>/<model>/` | create, through the admin form + inline formsets |
| `GET /api/v1/<app>/<model>/<pk>/` | detail: fieldsets (or `api_detail_fields`) + inlines |
| `PATCH`/`PUT /api/v1/<app>/<model>/<pk>/` | update; `{"inlines": {"items": [{"pk": 1, "quantity": 2}, {"pk": 2, "_delete": true}, {...new}]}}` |
| `DELETE /api/v1/<app>/<model>/<pk>/` | `delete_model` + LogEntry; 409 when protected |
| `GET /api/v1/<app>/<model>/<pk>/delete-preview/` | the admin's delete confirmation data as JSON |
Design: [docs/DESIGN.md](docs/DESIGN.md).

## Development

```bash
uv sync
uv run pytest
```
