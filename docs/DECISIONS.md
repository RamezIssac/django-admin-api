# Decision index

The decisions that steer this project live, with their reasoning and the rejected options, in
[DESIGN.md](DESIGN.md). This index is the map: one line per decision and the tests that
enforce it. Ids are as cited in the design doc (plain numbers and `R…` come from the design
rounds; `R3.…` are the round-3 decisions of §13; section-titled rows group decisions that the
doc records per milestone). The test suite is the executable form of these decisions — a
behaviour change without its test is unfinished.

| Id | Decision (one line) | Design | Enforced by |
|---|---|---|---|
| 8 | Query parameters use the admin format (`?field__lookup=`, `?q=`, `?o=`, `?p=`); reserved API params are the explicit set `{_shape, _per_page}` | [§5.1](DESIGN.md#51-list-apichangelist) | `tests/test_changelist.py` |
| 10 | List responses are lean (`count`, `results`, page links); columns, filters, actions and form fields live in `/schema/` | [§5.2](DESIGN.md#52-response-decision-10-lean--schema) | `tests/test_changelist.py` |
| R4 | FKs encode as `{pk, str}`; query count must not grow with the row count (N+1 guards) | [§5.4](DESIGN.md#54-foreign-keys-and-query-budget-decision-r4) | query-count tests in `tests/test_changelist.py`, `tests/test_detail.py` |
| 11a | One write pipeline — `process_changeform(request, obj, change)` shared by the HTML admin and the API; API JSON is translated into change-form POST data | [§6.1](DESIGN.md#61-the-pipeline-refactor-decision-11a-as-built), [§6.2](DESIGN.md#62-request-handling-around-the-pipeline) | `tests/test_writes.py` (incl. HTML↔API parity) |
| 12 | Inline payload: formset-prefix keys; a row with `pk` updates, `"_delete": true` deletes, no `pk` creates; unmentioned rows are kept | [§6.3](DESIGN.md#63-inline-payload-decision-12) | `tests/test_writes.py` |
| 23 | Reversion: the API opens `create_revision` itself, so `VersionAdmin` records revisions as in the HTML admin | [§6.2](DESIGN.md#62-request-handling-around-the-pipeline) | `tests/test_writes.py` |
| 13 | Actions run as POST with admin semantics; an action returning an `HttpResponse` is rolled back (`400 action_not_supported`) unless it has an `api_<name>` twin | [§7](DESIGN.md#7-actions-decision-13-as-built) | `tests/test_actions.py` |
| R5 | Delete goes through `get_deleted_objects()`; protected relations → 409; the confirmation data is available as JSON (`delete-preview/`) | [§8](DESIGN.md#8-delete-decision-r5) | `tests/test_detail.py` |
| 15, R6, R7 | Pluggable authenticators (session + token); a Bearer request is never authenticated by the session; only the token hash is stored | [§9.1–9.2](DESIGN.md#9-authentication-decisions-15-r6-r7) | `tests/test_tokens.py` |
| §9.3 | Token scopes only narrow the user's permissions; enforced in the mixin, in scoped inlines, and again in the service layer | [§9.3](DESIGN.md#93-token-scopes-milestone-10-as-built) | `tests/test_scopes.py` |
| R3.1 | Object rows by default; `?_shape=columnar` as opt-in; MCP always requests columnar | [§5.3](DESIGN.md#53-columnar-your-open-question-recommendation), [§13](DESIGN.md#13-decisions-from-round-3) | `tests/test_changelist.py`, `tests/test_mcp_tools.py` |
| R3.2 | `PrepopulatedAdmin` initial-data logic is not applied on API writes (initials are a UI concern) | [§6.3](DESIGN.md#63-inline-payload-decision-12), [§13](DESIGN.md#13-decisions-from-round-3) | `tests/test_writes.py` |
| R3.3 | `request.is_api` is set by `ApiRequestMiddleware` on every request; system check `admin_api.E001` fails loudly when the middleware is missing | [§13](DESIGN.md#13-decisions-from-round-3) | `tests/test_site.py` |
| R3.4 | Twin routing happens inside Django's getters (`ApiAdminMixin`); admins without the mixin get a cached runtime subclass `(ApiAdminMixin, TheirAdmin)` | [§13](DESIGN.md#13-decisions-from-round-3) | `tests/test_resolver.py` |
| MCP (§14) | Both transports (Streamable HTTP + stdio) on the official `mcp` SDK (core dependency; `ADMIN_API_MCP_ENABLED = False` turns the endpoints off); generic tools with `dry_run`, delete `confirm`, site `read_only`; API tokens only (OAuth later); JSON Schema + `structuredContent` | [§14](DESIGN.md#14-mcp-milestones-915-as-built) | `tests/test_mcp_tools.py`, `tests/test_mcp_http.py`, `tests/test_mcp_stdio.py`, `tests/test_mcp_conformance.py` |
| §14.7 | MCP writes return the document's `totals` (admin `api_totals`, read after a reload), an optional `require_dry_run` makes a real write need the `confirm_token` of a matching dry run (single-use, stored server side, bound to user, API token, arguments and object state; permissions checked first), every MCP call is logged to `admin_api.mcp.audit`, and MCP LogEntry messages end in `Via MCP.` | [§14.7](DESIGN.md#147-write-safety-010a2) | `tests/test_mcp_write_safety.py` |
| §14.4 | Write `input_schema` (JSON Schema draft 2020-12) is generated from the admin form and inline formsets, exposed on `/schema/` and MCP | [§14.4](DESIGN.md#144-json-schema) | `tests/test_jsonschema.py` |
| §15 | Nested admins: one path drives REST and MCP (`model` path + `parents`); every ancestor must be viewable, scopes included; django-subadmin stays optional and unmodified | [§15](DESIGN.md#15-nested-admins-milestone-17-as-built) | `tests/test_nested.py`, `tests/test_subadmin.py` |
| §16 | Custom admin views are declared once with `@admin_api.view` and exposed to REST and MCP with permissions, write semantics, MCP `dry_run` and optional promoted tools | [§16](DESIGN.md#16-custom-views-milestone-18-as-built) | `tests/test_custom_views.py` |
