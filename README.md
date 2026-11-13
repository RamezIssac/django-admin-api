# admin_api

[![CI](https://github.com/RamezIssac/django-admin-api/actions/workflows/ci.yml/badge.svg)](https://github.com/RamezIssac/django-admin-api/actions/workflows/ci.yml)

A thin layer that makes Django `ModelAdmin` API-ready and MCP-ready.

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

Status: **working prototype**: REST API (milestones 1–8) and MCP add-on (9–15). Tested on
Django 4.2, 5.2 and 6.1.
The mixin is optional: admins without it are wrapped at runtime for API requests.

### With erp_framework

```python
# urls.py, after INSTALLED_APPS/MIDDLEWARE as above
from erp_framework.api import erp_api_site

urlpatterns += [path("api/", erp_api_site.urls)]
```

ERP writes run `EntityAdmin.process_changeform`, the same pipeline as the HTML change form.
`examples/ra_erp_smoke/` exercises a full create/update/delete of a ra_erp `Sales` with item
lines.

| Endpoint | |
|---|---|
| `GET /api/v1/` | apps/models the user may use, with permissions |
| `GET /api/v1/<app>/<model>/` | changelist: admin filters (`?status__exact=`), search `?q=`, ordering `?o=`, page `?p=`, `_per_page`, `_shape=columnar` |
| `GET /api/v1/<app>/<model>/schema/` | columns, filters with their params, search, ordering, pagination, actions, `input_schema` (JSON Schema for create/update) |
| `POST /api/v1/<app>/<model>/` | create, through the admin form + inline formsets |
| `GET /api/v1/<app>/<model>/<pk>/` | detail: fieldsets (or `api_detail_fields`) + inlines |
| `PATCH`/`PUT /api/v1/<app>/<model>/<pk>/` | update; `{"inlines": {"items": [{"pk": 1, "quantity": 2}, {"pk": 2, "_delete": true}, {...new}]}}` |
| `DELETE /api/v1/<app>/<model>/<pk>/` | `delete_model` + LogEntry; 409 when protected |
| `GET /api/v1/<app>/<model>/<pk>/delete-preview/` | the admin's delete confirmation data as JSON |
| `POST /api/v1/<app>/<model>/actions/<name>/` | run an admin action on `{"pks": [...]}` or `{"all": true}` (+ filters); `api_<name>` twin for actions that return files/pages |
Design: [docs/DESIGN.md](docs/DESIGN.md).

## Authentication

* **Session**: same login as the admin. CSRF is enforced on unsafe methods.
* **Personal access tokens**: `Authorization: Bearer aapi_<prefix>_<secret>`. Create them in the
  admin ("API tokens", registered on the wrapped site) or with
  `manage.py create_api_token <username> --name ci`. Only a hash is stored. A token has the
  permissions of its user, optionally **narrowed by scopes**:
  `--scope store.sales:view --scope 'cash.*:*'` (`<app>.<model>:<view|add|change|delete>`, `*`
  allowed). Scopes never grant more than the user has.

## MCP

Let an AI agent (Claude Code, Claude Desktop, any MCP client) use the admin, with the same
permissions, validation, ERP hooks and audit trail as the HTML admin.

```bash
pip install "django-admin-api[mcp]"     # the official MCP SDK; the REST API doesn't need it
```

```python
# urls.py
from admin_api.mcp import AdminMcpSite

mcp_site = AdminMcpSite(api_site)            # AdminMcpSite(api_site, read_only=True) for reads only
urlpatterns += [path("mcp/", mcp_site.urls)]  # Streamable HTTP
```

```bash
# a token for the agent (scopes recommended)
python manage.py create_api_token alice --kind mcp --name claude --scope 'store.*:view'

# local, stdio
claude mcp add erp -e ADMIN_API_TOKEN=aapi_… -- python manage.py mcp_serve
# remote, HTTP
claude mcp add --transport http erp https://erp.example.com/mcp/ --header "Authorization: Bearer aapi_…"
```

Tools: `list_models`, `describe_model` (columns, filters, actions, JSON Schema),
`list_objects` (admin filters, search, ordering, paging), `get_object`, `create_object` /
`update_object` (with `dry_run`: the full save pipeline, then rolled back), `delete_object`
(preview unless `confirm=true`), `run_action`.

On the admin: `mcp_description = "…"` (context for the LLM), `mcp_enabled = False` (hide from
MCP only). In hooks: `request.is_mcp`, and `request.is_dry_run` to skip side effects such as
e-mails during a dry run (or use `transaction.on_commit`, which a dry run discards).

## Nested admins (django-subadmin)

Admins nested with [django-subadmin](https://github.com/inueni/django-subadmin) work in the REST
API and in MCP with no configuration. The address follows the admin URL:

| | items of sale 7 of client 1 |
|---|---|
| HTML admin | `/admin/store/client/1/sales/7/salesitem/` |
| REST | `/api/v1/store/client/1/sales/7/salesitem/` (+ `schema/`, `actions/<name>/`, `<pk>/`, `<pk>/delete-preview/`) |
| MCP | `model="store.client/sales/salesitem"`, `parents=[1, 7]` |

Lists are scoped to the parent, the parent FK is set on create (and is not accepted in payloads),
and every ancestor must be viewable by the user (and by the token's scopes). The registry /
`list_models` list nested admins with `nested_under`; object details and `/schema/` /
`describe_model` link to `children`. `api_enabled = False` / `mcp_enabled = False` on a
`SubAdmin` hides it and everything nested under it. Other nesting libraries can plug in with a
`NestedAdapter` (`admin_api.nested`).

## Development

```bash
uv sync
uv run pytest                    # everything; -m "not conformance" skips the live-server/subprocess tests
uvx nox                          # lint + matrix: Django 4.2 / 5.2 / 6.1 x mcp 2.3.0 / latest (+ django-subadmin on 5.2 / 6.1)
uvx nox -l                       # list sessions; run one with -s "tests(django=6.1, mcp=latest)"
```
