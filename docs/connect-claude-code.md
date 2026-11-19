# Connect Claude Code to the MCP server

Claude Code talks to the admin through the MCP server that `AdminMcpSite` provides. It acts as the
user the token belongs to, with that user's permissions, further narrowed by the token's scopes.

Every command on this page was run against a throwaway project with the installed `claude` CLI.
Tokens below are placeholders (`aapi_...`).

## What you need

1. `pip install --pre django-admin-api-mcp` in the project (the import name is `admin_api`).
2. An `AdminMcpSite` in the URLconf:

   ```python
   # urls.py
   from admin_api.mcp import AdminMcpSite

   mcp_site = AdminMcpSite(api_site)   # AdminMcpSite(api_site, read_only=True) for reads only
   urlpatterns += [path("mcp/", mcp_site.urls)]
   ```

3. A bearer token for a user who may use the admin (a staff user, as for the HTML admin).

Note: use a bearer token and not a session cookie, because the MCP endpoint only accepts tokens.

## Create tokens

A token has its user's permissions. `--scope` narrows it. The syntax is
`<app_label>.<model_name>:<view|add|change|delete>`, and `*` is allowed in any part. `change`
implies `view`. A scope never grants more than the user has. A token without scopes has all of
its user's permissions.

```bash
# read-only: every model of the store app
python manage.py create_api_token alice --kind mcp --name claude-read --scope 'store.*:view'

# read and write: view everything in store, add and change sales
python manage.py create_api_token alice --kind mcp --name claude-write \
    --scope 'store.*:view' --scope 'store.sales:add' --scope 'store.sales:change'
```

The command prints the token once (`aapi_<prefix>_<secret>`). Only a hash is stored, so copy it
now. Add `--expires-days 30` for a token that lapses. An invalid scope is rejected with the
expected syntax.

Note: create the agent a token of its own with the narrowest scopes it needs, because revoking
it then leaves your other tokens alone. Tokens can also be created and revoked in the admin
under "API tokens".

## Connect over HTTP

For a server that is already running (local or remote):

```bash
claude mcp add --transport http erp https://erp.example.com/mcp/ \
    --header "Authorization: Bearer aapi_..."
```

For a local dev server use `http://127.0.0.1:8000/mcp/`.

## Connect over stdio

Claude Code starts the Django process itself and talks to it over stdin and stdout. Run the
command from the project directory, or give absolute paths:

```bash
claude mcp add erp -e ADMIN_API_TOKEN=aapi_... -- python manage.py mcp_serve
```

If the project has more than one `AdminMcpSite`, add `--site path.to.mcp_site` after
`mcp_serve`. `--user alice` replaces the token only with `DEBUG=True`.

Note: pass the token with `-e ADMIN_API_TOKEN=...` and not `--token`, because command line
arguments show up in the process list. If the settings module or a virtualenv is not picked up,
add `-e DJANGO_SETTINGS_MODULE=myproject.settings` and use the full path to the virtualenv's
`python`.

Put the `claude mcp add` options (`-e`, `--header`, `--transport`, `--scope`) before the `--`:
everything after it belongs to the server command.

## Choose where the registration lives

`claude mcp add --scope <scope>` decides who sees the server:

| Scope | Stored in | Visible to |
| --- | --- | --- |
| `local` (default) | `~/.claude.json`, keyed by project | you, in this project only |
| `project` | `.mcp.json` at the project root | everyone who clones the repo |
| `user` | `~/.claude.json` | you, in every project |

For a shared repo use `project`, and keep the token out of the file. Claude Code expands
`${VAR}` in `.mcp.json`:

```bash
claude mcp add --transport http --scope project erp https://erp.example.com/mcp/ \
    --header 'Authorization: Bearer ${ADMIN_API_TOKEN}'
```

```json
{
  "mcpServers": {
    "erp": {
      "type": "http",
      "url": "https://erp.example.com/mcp/",
      "headers": { "Authorization": "Bearer ${ADMIN_API_TOKEN}" }
    }
  }
}
```

Each person exports their own `ADMIN_API_TOKEN` before starting `claude`. Single quotes keep the
shell from expanding the variable, so the file holds the literal `${ADMIN_API_TOKEN}`.

Note: commit `.mcp.json` with the variable reference and not the token, because the repository
history keeps every committed secret. Claude Code asks each user to approve a project server
the first time (`claude mcp list` shows `Pending approval` until then).

## Check the connection

```bash
claude mcp list
```

```
erp: https://erp.example.com/mcp/ (HTTP) - ✔ Connected
```

Inside a session, `/mcp` shows the server and its tools. The server offers `list_models`, `describe_model`, `list_objects`,
`get_object`, `create_object`, `update_object`, `delete_object`, `run_action`, `run_view`, plus
one tool per view published with `mcp_tool=`. A `read_only=True` site lists only the read tools.

## A first session

Ask in plain words. Claude Code calls the tools and asks you before the first use of each.

1. "List the models I can use on the erp server."
   It calls `list_models`, which shows each model with your `view`, `add`, `change` and
   `delete` rights.
2. "Show the five newest sales." It calls `describe_model` for the filters and columns, then
   `list_objects` and `get_object`.
3. "Create a client named Zeta. Run it as a dry run first." It calls `create_object` with
   `dry_run=true`: the whole save pipeline runs (validation, hooks, signals, `totals` when the
   admin declares `api_totals`), then the transaction is rolled back. Check the result, then
   say "go ahead" and it repeats the call without `dry_run`.

Note: ask for the dry run in the prompt, or set `AdminMcpSite(api_site, require_dry_run=True)`,
because a plain `create_object` writes at once. With `require_dry_run`, a real write is refused
unless it carries the single-use `confirm_token` that a dry run of the same arguments returned
(rules in the [README](../README.md)). `delete_object` shows a preview until `confirm=true`.

Each saved change appears in the admin history as `Changed <field>. Via MCP.`

To approve the tools without a prompt each time, add the server to your permissions, for
example `claude --allowedTools "mcp__erp"` or `"permissions": {"allow": ["mcp__erp"]}` in
`.claude/settings.json`. For a write token, allow the read tools and keep prompts for the write
tools.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `claude mcp list` does not show `Connected`, or a call returns 401 `invalid_token` | The token is wrong, revoked or expired, or the header is missing. The raw token cannot be recovered: create a new one. Check the header reads `Authorization: Bearer aapi_...` |
| 403 `permission_denied` on a tool call | The user or the token scopes do not allow the operation. `list_models` shows the rights per model. Add the scope (for example `--scope store.sales:add`) on a new token |
| 403 `origin_not_allowed` | The request carried a browser `Origin` that is not the server's own host. Add the origin to `CSRF_TRUSTED_ORIGINS`. Claude Code sends no `Origin` |
| 404 `not_found`, "No API for app.model" | The model has no admin API twin: it is not registered, not exposed, or has `api_enabled = False` or `mcp_enabled = False`. Registering it with `ApiAdminMixin` exposes it |
| 400 `Invalid HTTP_HOST header` | Add the public host name to `ALLOWED_HOSTS`. Behind a reverse proxy, forward the `Host` header, or set `USE_X_FORWARDED_HOST = True` |
| 405 `method_not_allowed` | The endpoint accepts POST only. A browser visit to `/mcp/` shows this |
| Stdio server will not start | Run `python manage.py mcp_serve` by hand. `Give --token, set $ADMIN_API_TOKEN` means the variable did not reach the process; `Invalid, revoked or expired token` means the token is bad |
| The token is gone | It is shown once at creation. Create another and revoke the old one in the admin |

Note: run the stdio command by hand while debugging, with the server's log on stderr and the
protocol on stdout, because anything else printed to stdout breaks the client.

## Other clients

The server is a standard MCP server, so any client that speaks Streamable HTTP can use the same
URL and `Authorization: Bearer aapi_...` header, and any client that starts a process can use
`python manage.py mcp_serve` with `ADMIN_API_TOKEN` in its environment.

Claude Desktop starts local servers from `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "erp": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/project/manage.py", "mcp_serve"],
      "env": {
        "ADMIN_API_TOKEN": "aapi_...",
        "DJANGO_SETTINGS_MODULE": "myproject.settings"
      }
    }
  }
}
```

Note: use the full path to the virtualenv's `python`, because Claude Desktop does not start in
your project directory or activate your environment.

## Worked example

A project with a tracker app (tasks, streams, statuses) mounts `/mcp/` on its `AdminMcpSite`.
A read token is scoped to `tracker.*:view`, and a write token adds `tracker.task:change`.

- "Which tasks are open in the Billing stream?" runs `describe_model` and `list_objects` with
  the admin's own filters.
- "Move task 42 to review." runs `update_object` with `dry_run=true`. The result shows the saved
  document and its `totals`. After the approval, the same call without `dry_run` saves it and
  the history reads `Changed Status. Via MCP.`
- With the read token the same request returns 403 `permission_denied`, and nothing changes.
