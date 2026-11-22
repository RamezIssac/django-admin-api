# Put OAuth in front of the MCP endpoint

`AdminMcpSite` authenticates every HTTP request with the classes in its `authenticators` list. The
library ships one: `TokenAuthenticator`, for its own `aapi_` API tokens. It ships no OAuth server.

This page adds OAuth 2.1 with [django-oauth-toolkit](https://django-oauth-toolkit.readthedocs.io/)
(DOT) as the authorization server. With it, a claude.ai custom connector, Claude Desktop, Claude
Code or any MCP client signs the user in through your Django login. Then it calls `/mcp/` with an
OAuth access token, and the server acts as that user.

Everything below was run against the library's test project with django-oauth-toolkit 3.4.1 and
Django 5.2. The run covered client registration, sign-in with PKCE, the token exchange and the
`/mcp/` calls.

## Who does what

| Piece | Owner |
| --- | --- |
| Sign-in page, consent screen, client registration (DCR, CIMD), access and refresh tokens | DOT |
| Discovery documents (RFC 9728 protected resource, RFC 8414 authorization server) | DOT |
| Access token to user and scopes | your `OAuthAuthenticator` (below) |
| `401` that points the client at the discovery document | your `OAuthMcpSite` (below) |
| Admin permissions, scope narrowing, `has_permission`, `Origin` check, dry runs, audit log | admin_api, unchanged |

## The hook: `authenticators`

`AdminMcpSite.authenticators` is a list of classes. For each request the site instantiates them
in order, with no arguments, and calls `authenticate(request)`. Each one does one of three things:

- returns a user: the request runs as that user.
- returns `None`: the next authenticator tries.
- raises an `ApiError`: the request ends with that error.

If none returns a user, the answer is `401 not_authenticated`.

The library reads token scopes from `request.api_token.scopes`, a list in its scope syntax
(`<app_label>.<model_name>:<view|add|change|delete>`, `*` allowed). The dry-run confirm token
pins `request.api_token.pk`, and the audit log records `request.api_token.prefix`. An
authenticator that sets nothing there gives the user's full permissions.

`TokenAuthenticator` answers `401 invalid_token` for every `Bearer` value it does not recognise.

Note: put `OAuthAuthenticator` before `TokenAuthenticator` and not after it, because
`TokenAuthenticator` rejects any unknown bearer value before a later authenticator sees it.

`AdminApiSite.authenticators` is the same hook for the REST API.

## 1. Install and configure django-oauth-toolkit

```bash
pip install "django-oauth-toolkit>=3.4"
```

```python
# settings.py
INSTALLED_APPS += ["oauth2_provider"]

MCP_URL = "https://erp.example.com/mcp/"   # exactly what users paste into Claude

OAUTH2_PROVIDER = {
    # OAuth scope names are library scopes: the token gets exactly these.
    "SCOPES": {
        "store.*:view": "Read every store record",
        "store.sales:change": "Change sales",
    },
    "PKCE_REQUIRED": True,
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,          # S256 only
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,  # header only, no ?access_token=
    # Claude registers itself: DCR (RFC 7591) and Client ID Metadata Documents.
    "DCR_ENABLED": True,
    "DCR_REGISTRATION_PERMISSION_CLASSES": ("oauth2_provider.dcr.AllowAllDCRPermission",),
    "CIMD_ENABLED": True,
    "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": ["none", "client_secret_post", "client_secret_basic"],
    "ALLOW_LOCALHOST_LOOPBACK": True,   # Claude Code: http://localhost:<any port>/callback
    # RFC 9728 document for the MCP endpoint.
    "OAUTH2_PROTECTED_RESOURCE_IDENTIFIER": MCP_URL,
    "OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS": ["https://erp.example.com/o"],
    "OAUTH2_PROTECTED_RESOURCE_NAME": "ERP admin",
}
```

```bash
python manage.py migrate
```

Open registration (`AllowAllDCRPermission`) lets any client register itself. A registered client
gets a token only after a user signs in and approves it on the consent page, and `/mcp/` still
requires a staff user (`api_site.has_permission`).
Without open registration, create one confidential client per hosted Claude app in the DOT admin:
redirect URI `https://claude.ai/api/mcp/auth_callback`, grant "Authorization code". Users then
enter its client ID and secret under the connector's Advanced settings.

## 2. Authenticator and site

```python
# myproject/mcp_auth.py
from dataclasses import dataclass

from django.urls import URLPattern
from django.views.decorators.csrf import csrf_exempt
from oauth2_provider.oauth2_backends import get_oauthlib_core
from oauth2_provider.www_authenticate import build_bearer_challenge

from admin_api.auth import BaseAuthenticator, TokenAuthenticator
from admin_api.errors import PermissionDeniedError
from admin_api.mcp import AdminMcpSite


@dataclass(frozen=True)
class OAuthGrant:
    """What the library reads from ``request.api_token``: scopes, plus ids for confirm and audit."""

    pk: str
    prefix: str
    scopes: list


class OAuthAuthenticator(BaseAuthenticator):
    """``Authorization: Bearer <django-oauth-toolkit access token>``. Leaves ``aapi_`` tokens alone."""

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, raw = header.partition(" ")
        if scheme.lower() != "bearer" or raw.strip().startswith("aapi_"):
            return None
        valid, oauth_request = get_oauthlib_core().verify_request(request, scopes=[])
        if not valid or not oauth_request.access_token.resource or not oauth_request.user.is_active:
            return None  # TokenAuthenticator answers 401 invalid_token
        access_token = oauth_request.access_token
        scopes = access_token.scope.split()
        if not scopes:
            raise PermissionDeniedError("The OAuth token carries no scopes.", code="insufficient_scope")
        label = f"oauth:{access_token.pk}"
        request.api_token = OAuthGrant(pk=label, prefix=label, scopes=scopes)
        return oauth_request.user


def with_resource_metadata(view):
    @csrf_exempt
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if response.status_code == 401:
            metadata_url = request.build_absolute_uri(f"/.well-known/oauth-protected-resource{request.path}")
            response["WWW-Authenticate"] = build_bearer_challenge(request, resource_metadata_url=metadata_url)
        return response

    return wrapped


class OAuthMcpSite(AdminMcpSite):
    authenticators = [OAuthAuthenticator, TokenAuthenticator]

    def get_urls(self):
        return [
            URLPattern(p.pattern, with_resource_metadata(p.callback), p.default_args, p.name)
            for p in super().get_urls()
        ]
```

What each part does:

- `verify_request` checks the token in DOT: it exists, has not expired, its application is
  usable, and, for a token issued with a `resource` (RFC 8707), that the request URL falls under
  that resource.
- A token without a `resource` falls through to `401`. Claude always sends `resource`, so every
  token Claude holds names this server as its audience.
- The token's OAuth scopes become library scopes on `request.api_token`. A token with no scopes
  gets `403 insufficient_scope`, not the user's full permissions.
- The confirm token and the audit log see `oauth:<access token id>`.
- The library's own `401` carries `WWW-Authenticate: Bearer realm="mcp"`. `OAuthMcpSite`
  replaces it with `Bearer resource_metadata="https://erp.example.com/.well-known/oauth-protected-resource/mcp/"`.
  That pointer is how Claude finds the authorization server.

`aapi_` tokens keep working on the same endpoint.

## 3. URLs

```python
# urls.py
from django.urls import include, path
from oauth2_provider import urls as oauth2_urls

from myproject.mcp_auth import OAuthMcpSite

mcp_site = OAuthMcpSite(api_site)

urlpatterns += [
    path("o/", include("oauth2_provider.urls", namespace="oauth2_provider")),
    path("", include((oauth2_urls.metadata_urlpatterns, "oauth_metadata"))),   # /.well-known/...
    path("mcp/", mcp_site.urls),
]
```

The second line serves both discovery documents at the site root:

| URL | Document |
| --- | --- |
| `/.well-known/oauth-protected-resource/mcp/` | RFC 9728: `resource` (the MCP URL), `authorization_servers`, `scopes_supported` |
| `/.well-known/oauth-authorization-server/o` | RFC 8414 for the issuer `https://erp.example.com/o`: endpoints, `registration_endpoint`, `code_challenge_methods_supported: ["S256"]`, `client_id_metadata_document_supported` |

Note: mount the metadata patterns at the root and not only under `o/`, because clients look for
`/.well-known/...` at the root with the issuer or resource path appended.

Users sign in on DOT's consent page through `LOGIN_URL`. A project that only has the admin
login can set `LOGIN_URL = "admin:login"`.

## 4. Scopes

OAuth scope strings are library scopes. `*`, `.` and `:` are legal in OAuth scope names, so
`store.*:view` passes through unchanged. The rules are the token rules from
[Connect Claude Code](connect-claude-code.md#create-tokens): `change` implies `view`, and a scope
never grants more than the user's admin permissions.

Claude asks for every scope in `scopes_supported` (the protected resource document) and the user
approves them on the consent page. For a read-only connector, list only `view` scopes in
`SCOPES`.

Coarse scope names such as `erp:read` work too: map them to library scopes in
`OAuthAuthenticator` before it builds `OAuthGrant`, for example
`{"erp:read": ["store.*:view"]}`.

A tool call outside the token's scopes returns a tool result with
`{"error": "permission_denied", "status": 403}`. The HTTP status is 200, as for every tool error,
so it does not trigger an OAuth step-up.

## 5. What Claude expects

The connector flow from claude.ai, Claude Desktop, mobile and Claude Code, and the piece that
answers each step:

| Step | Request | Answered by |
| --- | --- | --- |
| 1 | `POST /mcp/` without a token gets `401` with `WWW-Authenticate: Bearer resource_metadata="..."` | `OAuthMcpSite` |
| 2 | `GET /.well-known/oauth-protected-resource/mcp/`: `resource` must equal the URL the user entered, first `authorization_servers` entry is used | DOT, `OAUTH2_PROTECTED_RESOURCE_*` |
| 3 | `GET /.well-known/oauth-authorization-server/o` | DOT |
| 4 | Registration: CIMD when `client_id_metadata_document_supported` is true and `none` is an auth method, otherwise DCR at `/o/register/` | DOT, `CIMD_ENABLED`, `DCR_ENABLED` |
| 5 | Browser to `/o/authorize/` with PKCE `S256`, `scope` and `resource=<MCP URL>`; user signs in and approves | DOT |
| 6 | `POST /o/token/` (form encoded) with the code and verifier; refresh later with the refresh token | DOT, refresh tokens rotate by default |
| 7 | `POST /mcp/` with `Authorization: Bearer <access token>` | `OAuthAuthenticator`, then the library |

Redirect URIs: the hosted apps use `https://claude.ai/api/mcp/auth_callback`. Claude Code uses
`http://localhost:<port>/callback` on a port that changes per session; `ALLOW_LOCALHOST_LOOPBACK`
lets DOT match it on any port.

Claude reaches the server from Anthropic's published egress IP range and gives discovery,
registration and token endpoints 10 seconds to answer. Claude's requirements:
[Authentication for connectors](https://claude.com/docs/connectors/building/authentication).
The protocol: [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

## 6. Connect

- claude.ai: Settings, Connectors, Add custom connector, URL `https://erp.example.com/mcp/`.
  Claude registers itself and opens your sign-in page. With a pre-registered client, enter its ID
  and secret under Advanced settings.
- Claude Code: `claude mcp add --transport http erp https://erp.example.com/mcp/`, then `/mcp` in
  a session to sign in. Without `--header`, Claude Code runs the OAuth flow itself.
- `python manage.py mcp_serve` (stdio) keeps using `aapi_` tokens. OAuth covers the HTTP endpoint.

## 7. Check it

```bash
curl -s https://erp.example.com/.well-known/oauth-protected-resource/mcp/
curl -s https://erp.example.com/.well-known/oauth-authorization-server/o
curl -si -X POST https://erp.example.com/mcp/ -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The first names the MCP URL as `resource`, the second lists `S256` and a `registration_endpoint`,
the third is a `401` with `WWW-Authenticate: Bearer resource_metadata="..."`.

## Behind a reverse proxy

The audience check and the `resource_metadata` URL are built from `request.build_absolute_uri()`.
Behind TLS termination set `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`, and
forward the `Host` header (or set `USE_X_FORWARDED_HOST = True`).

Note: check that `curl -si -X POST https://<host>/mcp/` shows an `https://` `resource_metadata`
URL and not `http://`, because a scheme or host mismatch fails the audience check and every OAuth
token gets `401 invalid_token`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Claude says it could not reach the server, and DOT logs no requests | The `401` has no `resource_metadata`, or `/.well-known/oauth-protected-resource/mcp/` is a 404. Mount `OAuthMcpSite` and the root metadata patterns |
| Sign-in works, then every call is `401 invalid_token` | `resource` in the token does not match the request URL (proxy scheme or host), the token was issued without `resource`, or `OAuthAuthenticator` comes after `TokenAuthenticator` |
| `403 insufficient_scope` on connect | The approved token has no scopes. Check `SCOPES` and what the consent page listed |
| `403 permission_denied` with a valid token | The user is not staff (`api_site.has_permission`). Other codes: [Connect Claude Code](connect-claude-code.md#troubleshooting) |
| Claude Code sign-in fails with a redirect URI error | Set `ALLOW_LOCALHOST_LOOPBACK = True`; register `http://localhost/callback` and `http://127.0.0.1/callback` on a pre-registered client |
