# Changelog

## 0.1.0a2 (2026-11-19)

- The project is renamed to `django-admin-api-mcp` on PyPI from this release. The import name
  `admin_api` is unchanged.
- MCP `create_object` / `update_object` (and their dry runs) return the saved document's
  `totals`, declared per admin with `api_totals`. The object is reloaded after the save, so
  values written by signals are included (REST writes get the same reload and a `totals` entry).
- `AdminMcpSite(require_dry_run=True)` and `mcp_require_dry_run` on an admin: a real write
  (create, update, write views) is refused unless it carries the `confirm_token` returned by a
  matching, valid dry run. New error codes `dry_run_required` and `invalid_confirm_token`.
- LogEntry messages of MCP creates and updates end in `Via MCP.`.
- `get_mcp_description(request)` hook on `ApiAdminMixin`: override it to return another attribute
  (e.g. the admin's `description`) as the MCP description. `mcp_description` keeps working.
- `confirm_token` is a reserved view parameter name, next to `pk` and `dry_run`.

## 0.1.0a1 (published)

First alpha. Published as `django-admin-api2` (the `django-admin-api` name on PyPI is taken
by an unrelated package); the import package is `admin_api`.

- REST API from existing `ModelAdmin`s: changelist with admin filters/search/ordering,
  detail, `/schema/`, writes through the admin's own change-form pipeline with inline
  formsets, actions, delete with preview (decisions 8, 10, R4, 11a, 12, 13, R5).
- `api_` twins and `request.is_api` for API-specific behaviour; mixin-less admins are wrapped
  at runtime (§3, R3.4).
- Session + personal-access-token auth, tokens narrowed by scopes (15, R6, R7, §9.3).
- MCP server (`[mcp]` extra): Streamable HTTP and stdio transports, generic tools with
  `dry_run`, JSON Schema write payloads, official-client conformance tests (§14).
- Nested admins via django-subadmin; custom admin views via `@admin_api.view` (§15, §16).

Decision ids and section numbers refer to [docs/DESIGN.md](docs/DESIGN.md); the index with
the enforcing tests is [docs/DECISIONS.md](docs/DECISIONS.md).
