# Smoke test: admin_api over ra_erp

End-to-end check against a real erp_framework project (ra_erp's store app: polymorphic
`Sales` with `TransactionAdmin` and an `ItemMovementItem` inline). It uses a token, then calls
registry, schema, create with inline rows, search, detail, PATCH (update + delete a row),
delete-preview, and delete. A second test runs the same flow through MCP (`erp_mcp_site`)
with the official MCP client: describe, dry run (nothing left behind), create, inline delete,
search, delete preview and confirmed delete.

```bash
uv venv /tmp/erp-venv && source /tmp/erp-venv/bin/activate
uv pip install -e "../../[mcp]" -e ../../../django-erp-framework -e ../../../django-slick-reporting \
    -e ../../../django-tabular-permissions -e ../../../ra_erp django-polymorphic
PYTHONPATH=.:../../../ra_erp DJANGO_SETTINGS_MODULE=ra_smoke_settings python -m django test ra_smoke_tests
```

Each request and response is printed.
