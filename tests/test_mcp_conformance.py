"""Conformance: the official MCP client against the real transports, in both protocol eras.

* HTTP: a live (threaded WSGI) server, ``mcp.client`` over Streamable HTTP.
* stdio: ``manage.py mcp_serve`` as a subprocess on a temporary SQLite file.

``mode="legacy"`` negotiates the initialize handshake (2025-11-25 with mcp 2.3);
``mode="auto"`` negotiates the stateless 2026-07-28 era when both sides support it.
"""

import os
import subprocess
import sys
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from admin_api.models import ApiToken
from tests.testapp.models import Client as Customer
from tests.testapp.models import Sales, SalesItem

pytestmark = pytest.mark.conformance
MODES = ["legacy", "auto"]
ROOT = Path(__file__).resolve().parent.parent


async def scenario(client, customer_pk) -> dict:
    """A full agent session: discover, describe, write with dry run, act, delete with confirm."""
    seen = {"protocol": client.session.protocol_version}
    seen["tools"] = sorted(t.name for t in (await client.list_tools()).tools)

    models = (await client.call_tool("list_models", {})).structured_content["models"]
    seen["models"] = sorted(m["model"] for m in models)
    described = (await client.call_tool("describe_model", {"model": "testapp.sales"})).structured_content
    seen["required"] = described["input_schema"]["create"]["required"]

    data = {"slug": "S-1", "client": customer_pk, "inlines": {"items": [{"product": "Milk", "price": "2.50"}]}}
    dry = await client.call_tool("create_object", {"model": "testapp.sales", "data": data, "dry_run": True})
    seen["dry_run"] = dry.structured_content["valid"]
    bad = await client.call_tool("create_object", {"model": "testapp.sales", "data": {**data, "slug": "X-1"}})
    seen["invalid"] = (bad.is_error, bad.structured_content["non_field_errors"])

    created = (await client.call_tool("create_object", {"model": "testapp.sales", "data": data})).structured_content
    pk = created["object"]["pk"]
    item_pk = created["object"]["inlines"]["items"][0]["pk"]
    updated = await client.call_tool(
        "update_object",
        {"model": "testapp.sales", "pk": pk, "data": {"inlines": {"items": [{"pk": item_pk, "quantity": 4}]}}},
    )
    seen["value_after_update"] = updated.structured_content["object"]["value"]

    listed = await client.call_tool(
        "list_objects", {"model": "testapp.sales", "filters": {"status__exact": "draft"}, "ordering": ["-slug"]}
    )
    seen["listed"] = [row[1] for row in listed.structured_content["rows"]]
    action = await client.call_tool("run_action", {"model": "testapp.sales", "action": "mark_posted", "pks": [pk]})
    seen["action"] = action.structured_content["messages"]

    preview = await client.call_tool("delete_object", {"model": "testapp.sales", "pk": pk})
    seen["preview_deleted"] = preview.structured_content["deleted"]
    deleted = await client.call_tool("delete_object", {"model": "testapp.sales", "pk": pk, "confirm": True})
    seen["deleted"] = deleted.structured_content["deleted"]
    return seen


EXPECTED = {
    "tools": [
        "create_object",
        "delete_object",
        "describe_model",
        "get_object",
        "list_models",
        "list_objects",
        "run_action",
        "update_object",
    ],
    "models": ["testapp.client", "testapp.sales"],
    "required": ["slug", "client"],
    "dry_run": True,
    "invalid": (True, ["Slug must start with S-."]),
    "value_after_update": "10.00",  # SalesAdmin.save_related: 4 x 2.50
    "listed": ["S-1"],
    "action": [{"level": "info", "text": "1 posted."}],
    "preview_deleted": False,
    "deleted": True,
}


def expected_protocol(mode):
    return "2025-11-25" if mode == "legacy" else "2026-07-28"


# --------------------------------------------------------------------------------------------
# Streamable HTTP
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
def test_http(live_server, transactional_db, mode):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_superuser("root", "r@x.io", "pw")
    _, raw = ApiToken.objects.create_token(user, "conformance", kind="mcp")
    customer = Customer.objects.create(name="Ahmed")

    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {raw}"})
        async with Client(streamable_http_client(f"{live_server.url}/mcp/", http_client=http), mode=mode) as c:
            return await scenario(c, customer.pk)

    seen = anyio.run(go)
    assert seen.pop("protocol") == expected_protocol(mode)
    assert seen == EXPECTED
    assert not Sales.objects.exists() and not SalesItem.objects.exists()


@pytest.mark.parametrize("mode", MODES)
def test_http_rejects_bad_token(live_server, transactional_db, mode):
    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": "Bearer aapi_dead_beef"})
        async with Client(streamable_http_client(f"{live_server.url}/mcp/", http_client=http), mode=mode) as c:
            await c.list_tools()

    with pytest.raises(Exception):  # noqa: B017 - the SDK surfaces the 401 as a transport error
        anyio.run(go)


# --------------------------------------------------------------------------------------------
# stdio (real subprocess)
# --------------------------------------------------------------------------------------------
SETUP = """
import django; django.setup()
from django.contrib.auth import get_user_model
from admin_api.models import ApiToken
from tests.testapp.models import Client
user = get_user_model().objects.create_superuser("root", "r@x.io", "pw")
print(Client.objects.create(name="Ahmed").pk, ApiToken.objects.create_token(user, "stdio", kind="mcp")[1])
"""


@pytest.fixture(scope="module")
def stdio_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("stdio")
    (tmp / "stdio_settings.py").write_text(
        "from tests.settings import *  # noqa\n"
        f"DATABASES = {{'default': {{'ENGINE': 'django.db.backends.sqlite3', 'NAME': {str(tmp / 'db.sqlite3')!r}}}}}\n"
    )
    env = {**os.environ, "PYTHONPATH": f"{tmp}{os.pathsep}{ROOT}", "DJANGO_SETTINGS_MODULE": "stdio_settings"}
    subprocess.run([sys.executable, "-m", "django", "migrate", "-v0"], env=env, check=True, cwd=ROOT)
    out = subprocess.run([sys.executable, "-c", SETUP], env=env, check=True, cwd=ROOT, capture_output=True, text=True)
    customer_pk, raw = out.stdout.split()
    return env, int(customer_pk), raw


def stdio_params(env, **extra_env):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "django", "mcp_serve", "--site", "tests.urls.mcp_site"],
        env={**env, **extra_env},
        cwd=str(ROOT),
    )


@pytest.mark.parametrize("mode", MODES)
def test_stdio(stdio_env, mode):
    env, customer_pk, raw = stdio_env

    async def go():
        async with Client(stdio_params(env, ADMIN_API_TOKEN=raw), mode=mode) as c:
            return await scenario(c, customer_pk)

    seen = anyio.run(go)
    assert seen.pop("protocol") == expected_protocol(mode)
    assert seen == EXPECTED


def test_stdio_refuses_bad_token(stdio_env):
    env, _, _ = stdio_env
    proc = subprocess.run(
        [sys.executable, "-m", "django", "mcp_serve", "--site", "tests.urls.mcp_site"],
        env={**env, "ADMIN_API_TOKEN": "aapi_dead_beef"},
        cwd=ROOT,
        capture_output=True,
        text=True,
        input="",
        check=False,
    )
    assert proc.returncode != 0 and "Invalid, revoked or expired token." in proc.stderr
    assert proc.stdout == ""  # nothing may pollute the protocol stream
