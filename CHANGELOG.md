# Changelog

## 0.1.0a4 (unreleased)

- **New MCP tool `resolve_entities`** (read-only, §14.9): names the user typed become object ids and model
  labels across every model the user can see. Each term returns an `objects` and a `models` list,
  each with `status` `unique`, `ambiguous` or `none` and ranked candidates (`match`, `confidence`,
  `matched_field`, `label` as `str(obj)`). Terms take `kind` (`any`, `objects`, `models`),
  `models` (a top-level `models` is the default for terms without their own), and `for_field` (a create/update relation field or `inlines.<prefix>.<field>`, with the
  field's `limit_choices_to`). Candidates come from each admin's `get_search_results` over its
  `get_queryset(request)`, with its permissions, token scopes and live search fields.
  The `objects` list names `searched_models` only when its status is `none` or with `verbose=true`.
- `AdminMcpSite.excluded_models` (and the `excluded_models=` argument): app labels or
  `app_label.model_name` that `resolve_entities` never searches. Default: `auth`, `sessions`,
  `contenttypes`, `admin`, `admin_api`.
- `AdminMcpSite.get_entity_backend()` / `entity_backend_class`: the backend that finds
  candidates. The default `LiveEntityBackend` uses the admin's own search on any database.
- The default MCP instructions gain line 6: resolve names with `resolve_entities` first; reads use
  any `unique` result, writes only an `exact` or `exact_code` one, `ambiguous` and `none` ask.
- `InvalidArguments` moved to `admin_api.errors`; `admin_api.mcp.handlers` still imports it.
- **MCP answers in business wording.** The default instructions open with "This server gives you
  the user's business records and documents" and gain a paragraph asking the agent to name kinds
  of records by `name` / `name_plural`, records by their label, and to say "ID" only when needed.
  Line 6 now asks to list each ambiguous candidate's label and the kind of record, and to pass
  resolved IDs as describe_model's filter for that kind of record (e.g. `client`), not as `search`
  text.
- The default MCP instructions are 2,035 characters (were 2,540 after the lines above), same rules.
  An `ambiguous` resolve now says to ask which one before running anything, not to run once per
  candidate.
- MCP `list_objects` with a `search` that finds nothing adds `searched_fields` (the admin's search
  fields), `id_filters` (the `list_filter` parameters that take a related record's ID, e.g.
  `client__id__exact`) and a `hint`.
- **MCP keys are `id` and `label`** (REST unchanged): object heads, inline rows, related records
  (`{"id", "label"}`), `list_objects` `columns[0]`, write results, delete previews, view results;
  `describe_model` `pk_field` is now `id_field`. `list_models` items start with `name`. The tool
  arguments are `id` and `ids`; `pk` and `pks` stay accepted as aliases for one release. An inline
  row sent with `id` updates that row, and `describe_model`'s update schema names that key `id`.
- Messages use verbose names and "ID", wrapped in `gettext`: `"No sales with ID 9."`,
  `"This connection may not change sales."`, `"No view 'x' on sales."`. Unknown, hidden and excluded models all answer `"This kind of record is not
  available here."` (was `No API for app.model` / `No MCP access to app.model`).
- `import admin_api` works without Django settings: the scope verbs and error default details are
  `gettext_lazy` strings.
- `PermissionDeniedError` has a default detail ("You do not have permission to do this."); the
  services pass one per operation (`"You may not change this sales."`). Promoted view tools say
  "(Same as run_view 'post' on sales.)" and "ID of the sales.".
- `AdminMcpSite.get_instructions(request)` (sent at `initialize`; the HTTP view builds one SDK
  `Server` per request) and `AdminMcpSite.get_language(request)` (default `None`: keep the active
  language), which wraps `call_tool` and `get_tools` in `translation.override`.
- **New opt-in app `admin_api.contrib.search`** (§14.10): a search index behind `resolve_entities`.
  One row per (object, search field) and per (model, label, language), text folded by `fold()`
  (accents, case, Arabic letter variants, digits, the Arabic article). Rows follow the MCP-exposed
  admins' local `search_fields` / `api_search_fields` and `Meta` labels under every `LANGUAGES` code.
  Saves update rows on commit; `post_migrate` refreshes the label rows. Hits are scoped like the live
  backend (exposure, permissions, token scopes, `excluded_models`, live search fields) and checked
  against the admin's `get_queryset(request)` inside the query. Run `migrate`, then
  `search_index ensure` on every deploy.
- Pluggable engines through `ADMIN_API_SEARCH` (`DEFAULT_ENGINE`, `ENGINES`), interface
  `engines.base.SearchEngine`. The built-in `db` engine (`admin_api_search_entry` table) runs on every
  database; on Postgres the migration installs `pg_trgm` and a GIN index, and a trigram pass adds
  `fuzzy` matches.
- Commands `rebuild_search_index [--model] [--models-only] [--batch-size]` and `search_index ensure`
  (fingerprint-gated rebuild; exits non-zero on a `C` / `POSIX` database ctype). System check
  `admin_api_search.W001` warns about that ctype.
- `admin_api.contrib.search.indexer.indexing_suspended()`: a context manager for bulk loads; saves
  inside it (this thread or task) skip the index until `rebuild_search_index` runs.
- Admin hooks `get_search_index_fields()`, `search_index_code_fields`, `search_aliases`.
- Installing the app sets `AdminMcpSite.entity_backend_class` to `SearchEntityBackend`; sites that
  set their own keep it. Backends receive `limit=` in `search_objects` and
  `search_models`.
- CI runs the search tests on Postgres 17 (nox session `tests_postgres`).
- **Per-user MCP instructions.** New model `admin_api.McpUserInstructions` (migration `0003`):
  one instructions text per user, set in the Django admin (`McpUserInstructionsAdmin`). The default
  `AdminMcpSite.get_instructions(request)` appends it to the site's instructions, cut at
  `instructions_max_length` (4,096) with a note.
- MCP `list_objects` pages through at most `AdminMcpSite.list_max_rows` records of a query (default
  1000, `None` for no cap). A larger query reports only the reachable `pages` and adds a `hint`
  pointing at reports, filters and search; a page past the cap answers `invalid_arguments`.
- **MCP dry runs validate only.** `dry_run=true` on `create_object` / `update_object` runs the
  permission checks, the admin's queryset and its form and inline validation, and saves nothing:
  no `save_model`, signal, `LogEntry` or e-mail, no transaction to roll back. A write view's dry
  run checks permissions and params without calling the view. The result is `dry_run`, `valid`
  and `errors` or `note`; `object` and `totals` come with the real write only.
  `writes.validate_changeform` is the validation half of `default_process_changeform`.
- **`get_mcp_confirm_state(request, obj)`** on the admin (default on `ApiAdminMixin`: `None`)
  adds state stored outside the object's row, such as the last row of a status log, to the
  `require_dry_run` confirm-token fingerprint. A concurrent change to that state now gets 409
  `invalid_confirm_token`.
- **MCP `run_action` with `delete_selected` needs `confirm=true`**, like `delete_object`. Without it
  the tool returns the selection's `count`, a `preview` (`to_delete`, `model_count`, `perms_needed`,
  `protected`, `can_delete`) and `confirm_required`, and deletes nothing. REST actions are unchanged.
- Action descriptions are formatted like the admin's action menu: `delete_selected` reads
  "Delete selected sales", not "Delete selected %(verbose_name_plural)s".

## 0.1.0a3 (2026-11-19)

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
