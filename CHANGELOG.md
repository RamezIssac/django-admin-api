# Changelog

## 0.1.0a1 (unreleased)

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
