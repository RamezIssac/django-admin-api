# Changelog

## 0.1.0a3 (unreleased)

- **MCP dry runs capture e-mail.** A dry run rolled back the database but still delivered mail
  sent from `save()`, a signal or a write view. Now every e-mail backend opened during a dry run
  collects its messages, and the result lists them as `suppressed_emails` (`recipients`,
  `subject`). Other non-database side effects still happen: run them from
  `transaction.on_commit`, which a dry run discards.
- **`get_mcp_confirm_state(request, obj)`** on the admin (default on `ApiAdminMixin`: `None`)
  adds state stored outside the object's row, such as the last row of a status log, to the
  `require_dry_run` confirm-token fingerprint. A concurrent change to that state now gets 409
  `invalid_confirm_token`.
- **The MCP `confirm_token` is single-use and bound to the object's state.** It was a stateless
  signature, valid 15 minutes for any number of writes: one dry run allowed repeated writes, from
  any API token of the same user, after the object changed. Now the dry run stores a random `jti`
  in the new `McpConfirmation` model (migration `admin_api.0002`) with a fingerprint of the user,
  the API token, the operation, the exact arguments and the object's stored row. The real write
  spends it in one conditional `UPDATE`, in the same transaction as the write (row read
  `FOR UPDATE`): a failed write leaves the token unspent. A replay, another API token, other
  arguments or a changed object get 409 `invalid_confirm_token`; a call that does not match leaves
  the token unspent. The dry run then confirm flow and the error codes are unchanged. Run
  `migrate`.
- MCP writes check permissions and token scopes before the confirm token: a user who may not
  write gets 403 `permission_denied`, not 409 `dry_run_required` / `invalid_confirm_token`.
- Free-form changelist lookups (REST `?field__lookup=` and MCP `filters`) must start at a field
  the API shows: the pk, `list_display` field columns, `list_filter` fields, `date_hierarchy` and
  the API detail fields (add and change form). Other local fields answer 400 `bad_lookup`.
  `api_lookup_fields` / `get_api_lookup_fields(request)` sets the list explicitly.
- MCP `tools/list` no longer crashes for a non-superuser: tool permissions are checked in the
  sync thread (they query the database).
- Every MCP tool call is logged at INFO to the `admin_api.mcp.audit` logger (tool, user pk, token
  prefix, IP, model, pk, view, action, pks, `all`, `dry_run`, `confirm`, outcome), without `data`,
  `params` or `confirm_token`.
- `AdminApiConfig.default_auto_field = "django.db.models.AutoField"`, matching migration 0001: a
  project with `DEFAULT_AUTO_FIELD = BigAutoField` no longer sees a pending `admin_api` migration.

## 0.1.0a2 (2026-11-19)

- The project is renamed to `django-admin-api-mcp` on PyPI from this release. The import name
  `admin_api` is unchanged.
- The official `mcp` SDK is a core dependency and the `[mcp]` extra is gone: install with
  `pip install --pre django-admin-api-mcp`.
- `ADMIN_API_MCP_ENABLED` setting (default `True`): when `False`, the MCP HTTP view answers 404
  `mcp_disabled` and `manage.py mcp_serve` exits with an error.
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
- MCP server: Streamable HTTP and stdio transports, generic tools with
  `dry_run`, JSON Schema write payloads, official-client conformance tests (§14).
- Nested admins via django-subadmin; custom admin views via `@admin_api.view` (§15, §16).

Decision ids and section numbers refer to [docs/DESIGN.md](docs/DESIGN.md); the index with
the enforcing tests is [docs/DECISIONS.md](docs/DECISIONS.md).
