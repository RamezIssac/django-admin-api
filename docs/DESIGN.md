# admin_api — Design (v1)

> A thin layer that makes an existing Django `ModelAdmin` API-ready, and later MCP-ready,
> **without a second admin class**. The `ModelAdmin` stays the single source of truth;
> the API differs from the HTML admin only where you write an `api_` twin.

Status: design, round 2 answers incorporated. Package: `admin_api`. Repo: `django-admin-api/`.

---

## 1. Principles

1. **One class, two surfaces.** HTML admin and JSON API are driven by the same `ModelAdmin`.
   No serializers, no parallel permission system.
2. **Reuse Django's machinery, don't re-implement it.** `ChangeList` for lists, `get_form()` +
   inline formsets for writes, `has_*_permission` for authorization, `LogEntry` for audit.
3. **Override, don't configure.** API-specific behaviour comes from `api_` twins and
   `request.is_api`. These follow the same conventions as `list_display` / `get_list_display(request)`.
4. **Pure Django.** Only Django ≥ 4.2, Python ≥ 3.10. No DRF, no pydantic. Sync views.
5. **Exactly the same business rules.** A write through the API runs the same hooks as a
   save in the HTML admin, including erp_framework's `whole_changeform_validation`, `pre_save`,
   `post_save` and the `changeform_saved` signal, plus reversion.

### Why not the existing packages

| | django-api-admin | django-admin-rest-api | django-admin-mcp | **admin_api** |
|---|---|---|---|---|
| Same ModelAdmin | ✗ parallel `APIModelAdmin` | ✓ | ✓ mixin | ✓ |
| API-specific overrides | n/a | ✗ ("no new features") | few `mcp_*` flags | **api_ twins + `request.is_api`** |
| Validation | DRF serializers | `get_form()` | `get_form()` | `process_changeform()` pipeline |
| Runs ERP hooks | ✗ | ✗ | ✗ | ✓ |
| Uses `ChangeList` | own copy | reimplemented | reimplemented | ✓ |
| Tokens | DRF | ✗ session only | ✓ | ✓ (OAuth later) |

Ideas borrowed from django-admin-rest-api: delete-preview, nested `inlines` write block
through real formsets in one transaction, and the error envelope shape.

---

## 2. Architecture

```
                 ┌──────────────────────────── AdminSite (e.g. erp_admin_site) ─────────┐
                 │   ModelAdmin  ModelAdmin  ModelAdmin …   (registered once)            │
                 └───────────────▲──────────────────────────────────────────────────────┘
                                 │ wraps (no separate registration)
   /api/v1/… ──► AdminApiSite ───┤ auth → site.has_permission → resolve admin → view
                                 │
                     ┌───────────┴───────────┬──────────────┬───────────────┐
                 ListView              DetailView      WriteViews      ActionView …
                 (ApiChangeList)       (api_detail_     (process_       (get_api_actions)
                                        fields)          changeform)
                                 │
                       resolve(admin, "list_display", request)
                       get_api_X(request) → api_X → get_X(request) → X
```

### Components

| Component | Responsibility |
|---|---|
| `AdminApiSite` | Wraps an existing `AdminSite`. Global config: authenticators, page-size cap, version, `has_permission`. Generates URLs. Like `ERPFrameworkAdminSite`, it can be subclassed to change global behaviour. |
| `ApiAdminMixin` (optional) | Routes Django's getters to the `api_` twins when `request.is_api`, and adds `process_changeform()`. Admins **without** the mixin get it at runtime: `AdminApiSite.get_api_admin()` builds a cached subclass `(ApiAdminMixin, TheirAdmin)` for the API. |
| `resolver` | Single function implementing the twin lookup order (§3). |
| `ApiChangeList` | Subclass of `django.contrib.admin.views.main.ChangeList`, built from resolved api_ twins. |
| views | Plain Django function/class views returning `JsonResponse`. |
| `encoders` | Value → JSON (dates, decimals, FK objects, files, choices). |
| `auth` | Pluggable authenticator classes (session, token; OAuth later). |
| `ApiToken` model | Personal access tokens (§9). |

### Exposure

All admins on the wrapped site are exposed **by default**. To hide one:

```python
class InternalAdmin(admin.ModelAdmin):
    api_enabled = False
```

`AdminApiSite.is_exposed(model_admin)` is the hook for site-wide rules.

```python
# project urls.py
from admin_api import AdminApiSite
from erp_framework.sites import erp_admin_site

api_site = AdminApiSite(admin_site=erp_admin_site)  # version="v1" default
urlpatterns = [
    path("admin/", erp_admin_site.urls),
    path("api/", api_site.urls),  # → /api/v1/…
]
```

---

## 3. The `api_` convention

### Lookup order

For a hook `X` that has a twin:

```
get_api_X(request[, obj])  →  api_X  →  get_X(request[, obj])  →  X
```

The first one that is *defined* wins: a method overridden on the class, or an attribute
that is not `None`. Your Django code stays idiomatic and the API only diverges where you say.

### Hooks with twins (v1)

| Normal | Attribute twin | Method twin |
|---|---|---|
| `list_display` | `api_list_display` | `get_api_list_display(request)` |
| `list_filter` | `api_list_filter` | `get_api_list_filter(request)` |
| `search_fields` | `api_search_fields` | `get_api_search_fields(request)` |
| `ordering` | `api_ordering` | `get_api_ordering(request)` |
| `fields` / `fieldsets` | `api_fields` / `api_fieldsets` | `get_api_fields(request, obj)` / `get_api_fieldsets(request, obj)` |
| `readonly_fields` | `api_readonly_fields` | `get_api_readonly_fields(request, obj)` |
| `form` | `api_form` | `get_api_form(request, obj, change)` |
| `inlines` | `api_inlines` | `get_api_inlines(request, obj)` |
| `actions` | `api_actions` | `get_api_actions(request)` |
| `get_queryset` | — | `get_api_queryset(request)` |
| `list_per_page` | `api_list_per_page` | — |
| *(new)* | `api_detail_fields` | `get_api_detail_fields(request, obj)` |

### Everything else: `request.is_api`

`ApiRequestMiddleware` sets `request.is_api = False` on every request, and API views set it
to `True`. Any other hook can branch on it:

```python
def has_delete_permission(self, request, obj=None):
    if request.is_api:
        return False
    return super().has_delete_permission(request, obj)
```

### Example

```python
class SalesAdmin(TransactionAdmin):  # no mixin required
    list_display = ["slug", "date", "client", "get_stats_icon", "value"]
    api_list_display = ["slug", "date", "client", "balance", "value"]

    @admin_api.display(description="Balance")
    def balance(self, obj):
        return obj.get_balance()

    def get_api_queryset(self, request):
        return super().get_queryset(request).select_related("client", "store")
```

### Callables in `list_display`

Callables are **left out** of the API unless they are marked in one of two ways:
- (a) they are listed explicitly in `api_list_display`, or
- (b) they are decorated with `@admin_api.display(...)`.

That stops HTML-returning admin methods (`format_html`, icons) from leaking into JSON.
`@admin_api.display` behaves like `@admin.display` (description, ordering, boolean) and
also marks the method as safe for the API.

---

## 4. URL map

All URLs are under `<mount>/v1/`. The version comes from `AdminApiSite.version`.

| Method | Path | View | Permission |
|---|---|---|---|
| GET | `/` | registry: apps → models, with per-model perms | `site.has_permission` |
| GET | `/<app>/<model>/` | list | `has_view_permission` (or change) |
| GET | `/<app>/<model>/schema/` | columns, filters, actions, form fields, inlines | view |
| POST | `/<app>/<model>/` | create | `has_add_permission` |
| GET | `/<app>/<model>/<pk>/` | detail | `has_view_or_change_permission(obj)` |
| PATCH | `/<app>/<model>/<pk>/` | partial update | `has_change_permission(obj)` |
| PUT | `/<app>/<model>/<pk>/` | full update | `has_change_permission(obj)` |
| DELETE | `/<app>/<model>/<pk>/` | delete | `has_delete_permission(obj)` |
| GET | `/<app>/<model>/<pk>/delete-preview/` | cascade preview | `has_delete_permission(obj)` |
| POST | `/<app>/<model>/actions/<name>/` | run an action | action's `allowed_permissions` |
| — | `/reports/<slug>/` | **reserved** (slick_reporting, later) | — |
| — | `/mcp/` (outside `/v1/`) | **reserved** (MCP, later) | — |

---

## 5. Read side

### 5.1 List: `ApiChangeList`

The list is built by a `ChangeList` subclass constructed exactly like
`ModelAdmin.get_changelist_instance()`, but from the resolved twins:

```python
cl = ApiChangeList(
    request_for_changelist,
    model,
    list_display=resolve("list_display"),
    list_display_links=(),
    list_filter=resolve("list_filter"),
    date_hierarchy=admin.date_hierarchy,
    search_fields=resolve("search_fields"),
    list_select_related=admin.get_list_select_related(request),
    list_per_page=resolve("list_per_page"),
    list_max_show_all=admin.list_max_show_all,
    list_editable=(),
    model_admin=admin,
    sortable_by=admin.get_sortable_by(request),
    search_help_text=admin.search_help_text,
)
```

`ApiChangeList.get_queryset()` starts from `get_api_queryset(request)` instead of
`get_queryset(request)`, by overriding `root_queryset`.

**Query parameters use the admin format** (decision 8): filters as `?client__id__exact=2`,
search as `?q=`, ordering as `?o=2.-1`, page as `?p=`, date hierarchy as `?date__year=2025`.

**Reserved API parameters** are an explicit set, `{_shape, _per_page}`. They are removed
from `request.GET` before `ChangeList` sees it, because `ChangeList` treats every unknown GET
key as a lookup. The prefix `_` alone can't be the rule: the admin owns `_facets`, `_popup`
and `_to_field`. `_per_page` is capped by `AdminApiSite.max_page_size`.
`IncorrectLookupParameters`, `SuspiciousOperation` and out-of-range pages map to `400 bad_lookup`.

**Ordering** uses `?o=` with 0-based indexes into the API columns. Unmarked callables are
removed *before* the `ChangeList` is built, so the indexes match `/schema/`'s `order_index`.

### 5.2 Response (decision 10: lean + `/schema/`)

```json
GET /api/v1/store/sales/?q=S-00&p=1
{
  "count": 120, "page": 1, "pages": 3, "per_page": 50,
  "next": "/api/v1/store/sales/?q=S-00&p=2", "previous": null,
  "results": [
    {"pk": 7, "slug": "S-0007", "date": "2025-01-03",
     "client": {"pk": 2, "str": "Ahmed"}, "balance": "300.00", "value": "1500.00"}
  ]
}
```

Column labels, types, filter choices, actions and form fields live in `GET …/schema/`.

### 5.3 Columnar (your open question): recommendation

The columnar shape matches `list_display` well, since rows are already ordered columns:

```json
{"count": 120, "page": 1, "pages": 3,
 "columns": ["pk", "slug", "date", "client", "balance", "value"],
 "rows": [[7, "S-0007", "2025-01-03", {"pk": 2, "str": "Ahmed"}, "300.00", "1500.00"]]}
```

| | Object rows | Columnar |
|---|---|---|
| Payload size | baseline | ~40–60 % smaller on wide lists |
| LLM / MCP token cost | higher | **lower** |
| Generic HTTP clients, JS, Python | natural | must zip columns and rows |
| Mirrors `list_display` order | implicitly | explicitly |

**Proposal:** object rows by default, and `?_shape=columnar` as an opt-in. The MCP layer
(later) requests columnar. The detail endpoint always returns an object. → *Decided (round 3).*

### 5.4 Foreign keys and query budget (decision R4)

FKs are encoded as `{"pk": …, "str": …}`. `str` needs the related row, so:

- **List:** we reuse `ChangeList.apply_select_related()` unchanged. With
  `list_select_related = False` (the default), `ChangeList` already calls `select_related()`
  when a FK is in the (api_)list_display. Admins that set `list_select_related`
  explicitly keep control. M2M columns get `prefetch_related` added by `ApiChangeList`.
- **Detail:** FK and M2M fields in `api_detail_fields` are added to `select_related` /
  `prefetch_related` on `get_api_queryset()`.
- **`__str__` that touches more relations** remains the admin author's concern, the same
  as in the HTML changelist. `get_api_queryset` is the place to fix it.
- **Guarantee by test:** every endpoint has an `assertNumQueries` test proving that the query
  count does not grow with the number of rows (N+1 guard).
- Escape hatch: a custom encoder per field via `api_field_encoders = {"client": fn}`.

### 5.5 Detail

`api_detail_fields` sets the fields returned. Default: the fields of
`get_api_fieldsets(request, obj)`, flattened, plus `pk` and `str`. Readonly callables follow
the same marking rule as list callables. Inlines are included as nested lists, each read
through the inline's `get_queryset` and permission checks.

---

## 6. Write side

### 6.1 The pipeline refactor (decision 11a), as built

Today erp_framework's hooks run only inside `EntityAdmin._changeform_view`, which also renders
HTML. We extract the non-HTML part into a method that **both** surfaces call.

**Contract:** `process_changeform(self, request, obj, change) -> result`
- `request` is a POST request. The form data is in `request.POST` / `request.FILES`, exactly
  as the HTML change form submits it. For the API, this is a copy of the API request with
  `method = "POST"` and the JSON translated into change-form POST data (§6.2).
- `result` has attributes `valid`, `obj`, `form`, `formsets` and `change`
  (`admin_api.writes.ChangeformResult`, or any object with those attributes).
- The pipeline must not render or redirect. The caller does that: the HTML view renders the
  page or redirects, and the API serializes the object or the errors.

Because the API hands the pipeline a real POST request, every hook that reads
`request.POST` (`_create_formsets`, `get_formset_kwargs`, `save_formset`) behaves exactly as
in the HTML admin. No `data`/`files` arguments are needed.

`admin_api.writes.default_process_changeform(model_admin, request, obj, change)` mirrors
`ModelAdmin._changeform_view` minus rendering and is used when the admin defines no
`process_changeform`. The mixin deliberately does **not** define `process_changeform`: the
runtime subclass `(ApiAdminMixin, TheirAdmin)` must not shadow erp_framework's version.

erp_framework's `EntityAdmin.process_changeform` adds its hooks:

```python
def process_changeform(self, request, obj, change):
    ModelForm = self.get_form(request, obj, change=change)
    form = ModelForm(request.POST, request.FILES, instance=obj)
    formsets, inline_instances = self._create_formsets(request, form.instance, change=change)
    form_validated = form.is_valid()
    new_object = self.save_form(request, form, change=change) if form_validated else form.instance
    valid = all_valid(formsets) and form_validated and \
        self.whole_changeform_validation(request, form, formsets, change)
    if valid:
        self.pre_save(form, formsets, change)
        self.save_model(...); self.save_related(...); log_addition/log_change
        self.post_save(request, new_object, form, formsets, change)
        changeform_saved.send(...)
    return ChangeformResult(valid, new_object if valid else None, form, formsets, change, ...)
```

### 6.2 Request handling around the pipeline

```
parse JSON → reject unknown / readonly keys (400)
→ POST data: PATCH = current values (form initial) + payload; POST/PUT = add-form initial + payload
→ "inlines" block → formset POST data (management form, row ids, DELETE)  (§6.3)
→ post_request = copy(request) with method="POST", POST=data, in-memory message storage
→ with transaction.atomic():
     with admin.create_revision(post_request) if VersionAdmin else nullcontext():
         result = admin.process_changeform(post_request, obj, change)  # or the default
     if not result.valid: set_rollback(True)
→ 201/200 with detail payload (+ "messages" from message_user)  |  400 validation_failed | 409 conflict
```

Messages raised with `message_user` / `messages.*` during an API write are captured in
memory and returned in the response. They never reach the session, so they don't show up
later in the HTML admin.

**Reversion (decision 23):** `VersionAdmin` records revisions only by wrapping
`add_view`/`change_view` in `self.create_revision(request)`, so the API must open that
context itself. Its `log_addition`/`log_change` overrides then attach the comment, as
they do in the HTML admin.

### 6.3 Inline payload (decision 12)

```json
PATCH /api/v1/store/sales/7/
{
  "client": 2,
  "inlines": {
    "items": [
      {"product": 5, "quantity": 3, "price": "10.00"},
      {"pk": 41, "quantity": 1},
      {"pk": 42, "_delete": true}
    ]
  }
}
```

- The key is the formset **prefix** (`get_default_prefix()`), also listed in `/schema/`.
- Translated into real formset data: `items-TOTAL_FORMS`, `items-INITIAL_FORMS`,
  `items-N-id`, `items-N-DELETE`, …
- Row semantics (same for POST, PUT and PATCH): a row with `pk` updates that row, merged onto
  its current values; `"_delete": true` deletes it. A row without `pk` creates a new row.
  Existing rows that are **not mentioned are kept unchanged**: they are still submitted as
  initial forms with their current values, so formset-level validation sees the whole set.
- Inline permissions (`has_add/change/delete_permission` on the inline) are checked by the
  formset machinery, as in the HTML admin. `TransactionItemAdmin`'s overrides apply unchanged.
- erp_framework's `PrepopulatedAdmin` initial-data logic is **not** applied on API writes
  (initial values are a UI concern). → *Decided (round 3).*

### 6.4 Error envelope

```json
400 {"error": "validation_failed",
     "fields": {"client": ["This field is required."]},
     "non_field_errors": ["Total does not match items."],
     "inlines": {"items": {"rows": [{"index": 1, "pk": 41, "errors": {"quantity": ["…"]}}],
                           "non_form_errors": []}}}
401 {"error": "not_authenticated"}
403 {"error": "permission_denied"}
404 {"error": "not_found"}
400 {"error": "bad_lookup", "detail": "…"}
409 {"error": "conflict"}            # IntegrityError
```

---

## 7. Actions (decision 13)

`POST /api/v1/<app>/<model>/actions/<name>/` with body `{"pks": [1, 2, 3]}` or `{"all": true}`
(`all` honours the same list filters passed as query params).

- The action set comes from `get_api_actions(request)` → `api_actions` → `get_actions(request)`.
  Permissions come through `allowed_permissions`, exactly as in the admin.
- Messages sent with `message_user` during the action are captured, with a per-request message
  store, and returned as `{"messages": [{"level": "success", "text": "…"}]}`.
- An action that returns an `HttpResponse` (file export, confirmation page) is **not exposed**
  unless it has an API version: `api_<action_name>(modeladmin, request, queryset)` returning a
  dict, or one decorated with `@admin_api.action`. These actions are listed under `hidden` in
  `/schema/` with a reason.

## 8. Delete (decision R5)

- `DELETE …/<pk>/` → `has_delete_permission(obj)` → `get_deleted_objects()` check (protected
  relations → 409 with the list) → `delete_model()` → `log_deletion`.
- `GET …/<pk>/delete-preview/` → the same `get_deleted_objects()` output as JSON:
  `{"to_delete": [...nested str...], "model_count": {...}, "perms_needed": [...], "protected": [...]}`.

---

## 9. Authentication (decisions 15, R6, R7)

### 9.1 Authenticators

```python
class AdminApiSite:
    authenticators = [SessionAuthenticator, TokenAuthenticator]  # v1
    # later: OAuthAuthenticator (django-oauth-toolkit), added here, nothing else changes
```

Each authenticator returns `(user, auth_info)` or `None`. Then `site.has_permission(request)`
(is_active and is_staff by default, or the wrapped site's rule) is enforced.
`SessionAuthenticator` enforces CSRF for unsafe methods. `TokenAuthenticator` does not need CSRF.

### 9.2 `ApiToken`: built small, with room to grow

```python
class ApiToken(models.Model):
    user = FK(AUTH_USER_MODEL, related_name="api_tokens")
    name = CharField(max_length=100)  # "kheir mobile app"
    prefix = CharField(max_length=12, unique=True)  # shown in admin, used for lookup
    key_hash = CharField(max_length=128)  # sha256(secret); secret shown once
    created = DateTimeField(auto_now_add=True)
    expires_at = DateTimeField(null=True, blank=True)
    last_used_at = DateTimeField(null=True, blank=True)  # throttled update
    revoked_at = DateTimeField(null=True, blank=True)
    # --- reserved for later, present from day one so no data migration is needed ---
    scopes = JSONField(default=list, blank=True)  # [] = full user permissions (v1)
    kind = CharField(default="pat", max_length=20)  # "pat" | "mcp" | "service"
```

Header: `Authorization: Bearer aapi_<prefix>_<secret>`.

**Planned expansion:**
1. **Scopes** (v2): `has_*_permission` stays the ceiling. Scopes can only *narrow* it, e.g.
   `["store.view_sales", "store.*:read"]`. Enforcement adds one check in the view layer.
   `scopes=[]` keeps today's meaning.
2. **MCP tokens** (with MCP): `kind="mcp"` tokens are issued for stdio `mcp_serve --token`
   and shown with their own admin filter.
3. **OAuth 2.1** (with remote MCP): a separate authenticator backed by django-oauth-toolkit.
   `ApiToken` is untouched, and OAuth access tokens map to the same user and scopes model.
4. **Swappable model:** `ADMIN_API_TOKEN_MODEL` setting with `AbstractApiToken` base, in case
   a project needs extra columns (org, tenant).

Token admin: an `ApiTokenAdmin` on the wrapped site. Users create and revoke their own tokens,
and superusers manage all of them.

---

## 10. Out of scope for v1 (reserved in the design)

| Item | Reservation |
|---|---|
| Custom admin views (`view_view`, `get_by_slug`) | `@admin_api.view` decorator name reserved |
| slick_reporting reports | `/api/v1/reports/<slug>/` path reserved |
| MCP server | `/mcp/` mount + `manage.py mcp_serve`. Thin gate over the same views. Tool design deferred |
| JSON Schema / OpenAPI generation | `/schema/` is the seed. Formal schemas come with MCP |
| `list_editable` bulk edit | — |
| Async views | — |

---

## 11. Testing strategy

- **Parity tests:** the same payload via HTML admin POST and via API must give identical DB
  state, `LogEntry` rows and reversion versions.
- **Twin resolution tests:** every hook in §3 against all four lookup levels.
- **Query budget:** `assertNumQueries` on list/detail with 1 and 20 rows; the count must be equal.
- **Permission matrix:** anonymous / non-staff / staff-without-perm / staff-with-perm × every endpoint.
- **ERP integration:** in erp_framework's test suite, a `TransactionAdmin` with
  `whole_changeform_validation` returning `False` must reject the API write.

---

## 12. Prototype milestones

1. Skeleton: package, `AdminApiSite`, resolver, `request.is_api`, session auth, registry endpoint.
2. List: `ApiChangeList`, encoders, FK objects, `/schema/` (columns + filters).
3. Detail + delete + delete-preview.
4. Writes: default `process_changeform`, JSON → form data, inlines translation, errors, reversion.
5. erp_framework refactor: `EntityAdmin.process_changeform`, with `_changeform_view` calling it.
6. Actions with message capture.
7. `ApiToken` + `TokenAuthenticator` + token admin.
8. Demo against `my-shop` (optional, later).

---

## 13. Decisions from round 3

1. **Columnar:** object rows by default; `?_shape=columnar` opt-in. MCP uses columnar.
2. **PrepopulatedAdmin on API writes:** skipped.
3. **`request.is_api`:** set by `admin_api.middleware.ApiRequestMiddleware` (`False` on
   every request). API views set it to `True`. A system check (`admin_api.E001`) fails if the
   middleware is missing, so `request.is_api` can be used without `getattr`.
4. **Automatic routing in the mixin.** `ApiAdminMixin` overrides the normal Django getters
   (`get_form`, `get_fieldsets`, `get_fields`, `get_readonly_fields`, `get_inlines`,
   `get_list_display`, …) so that when `request.is_api` they return the resolved twin.

   *Why this matters:* Django's getters call each other internally. `get_form()` calls
   `get_fieldsets()`, which calls `get_fields()`, and `get_readonly_fields()` decides what is
   excluded. The rejected option was "API views call the resolver explicitly": the view would
   fetch `api_form` itself, but when Django then builds the form, its *internal* calls still
   hit the normal `get_fieldsets` and `get_readonly_fields`, so `api_readonly_fields` would be
   silently ignored. Routing inside the getters makes every internal call API-aware, and
   `process_changeform` needs no API branch.

   *Consequence (revised in milestone 2):* routing needs `ApiAdminMixin` in the MRO. Admins
   without it get a cached runtime subclass `(ApiAdminMixin, TheirAdmin)` from
   `AdminApiSite.get_api_admin()`, used only for API requests. All twins therefore work with
   or without the mixin, and the planned system check W001 was dropped.

   MRO note: with an explicit mixin (`class A(ApiAdminMixin, ModelAdmin)`), your own
   `get_X` override sits *above* the mixin and wraps the twin's result when it calls `super()`.
   With the runtime subclass, the mixin sits above your class, so a twin replaces your `get_X`
   for API requests. Both follow the lookup order: a twin beats `get_X`.
