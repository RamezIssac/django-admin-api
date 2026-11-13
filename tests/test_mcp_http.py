"""The Streamable HTTP endpoint (/mcp/), spoken as raw JSON-RPC through Django's test client."""

import json

import pytest
from asgiref.sync import async_to_sync
from django.test import AsyncClient, override_settings

from admin_api.models import ApiToken
from tests.testapp.models import Client, Sales

VERSION = "2025-06-18"
ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def token(superuser):
    return ApiToken.objects.create_token(superuser, "mcp", kind="mcp")[1]


def post(client, body, *, token=None, version=VERSION, **extra):
    headers = {"HTTP_ACCEPT": ACCEPT, **extra}
    if token:
        headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if version:
        headers["HTTP_MCP_PROTOCOL_VERSION"] = version
    return client.post("/mcp/", data=json.dumps(body), content_type="application/json", **headers)


def rpc(client, token, method, params=None, id=1):
    body = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    resp = post(client, body, token=token, version=None if method == "initialize" else VERSION)
    assert resp.status_code == 200, resp.content
    return json.loads(resp.content)


def call_tool(client, token, name, arguments):
    result = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})["result"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"], result["isError"]


def test_initialize(client, token):
    out = rpc(
        client,
        token,
        "initialize",
        {"protocolVersion": VERSION, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
    )["result"]
    assert out["protocolVersion"] == VERSION
    assert out["serverInfo"]["name"] == "testapp"
    assert out["capabilities"]["tools"] is not None
    assert out["instructions"].startswith("This server exposes a Django admin.")


def test_tools_list(client, token):
    tools = {t["name"]: t for t in rpc(client, token, "tools/list", {})["result"]["tools"]}
    assert len(tools) == 8
    assert tools["list_models"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_object"]["annotations"]["destructiveHint"] is True
    assert tools["create_object"]["inputSchema"]["required"] == ["model", "data"]


def test_tools_call_read_and_write(client, token, db):
    ahmed = Client.objects.create(name="Ahmed")
    out, err = call_tool(client, token, "list_models", {})
    assert not err and "testapp.sales" in {m["model"] for m in out["models"]}

    payload = {"slug": "S-1", "client": ahmed.pk}
    out, err = call_tool(client, token, "create_object", {"model": "testapp.sales", "data": payload, "dry_run": True})
    assert not err and out["valid"] is True
    assert not Sales.objects.exists()
    out, err = call_tool(client, token, "create_object", {"model": "testapp.sales", "data": payload})
    assert not err and Sales.objects.get().slug == "S-1"


def test_tool_error_is_a_result(client, token, db):
    out, err = call_tool(client, token, "get_object", {"model": "testapp.sales", "pk": 404})
    assert err is True and out["error"] == "not_found"


def test_scoped_token(client, superuser, db):
    _, raw = ApiToken.objects.create_token(superuser, "ro", scopes=["testapp.client:view"])
    out, _ = call_tool(client, raw, "list_models", {})
    assert [m["model"] for m in out["models"]] == ["testapp.client"]
    out, err = call_tool(client, raw, "list_objects", {"model": "testapp.sales"})
    assert err and out["error"] == "permission_denied"


# --------------------------------------------------------------------------------------------
# HTTP-level gatekeeping (before the SDK)
# --------------------------------------------------------------------------------------------
PING = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def test_requires_token(client, superuser):
    resp = post(client, PING)
    assert resp.status_code == 401 and resp["WWW-Authenticate"] == 'Bearer realm="mcp"'
    client.force_login(superuser)  # a session is not enough
    assert post(client, PING).status_code == 401


def test_invalid_token(client, db):
    resp = post(client, PING, token="aapi_dead_beef")
    assert resp.status_code == 401 and json.loads(resp.content)["error"] == "invalid_token"


def test_non_staff_token(client, plain_user):
    _, raw = ApiToken.objects.create_token(plain_user, "t")
    assert post(client, PING, token=raw).status_code == 403


def test_get_and_delete_not_allowed(client, token):
    for method in ("get", "delete"):
        resp = getattr(client, method)("/mcp/", HTTP_AUTHORIZATION=f"Bearer {token}")
        assert resp.status_code == 405 and resp["Allow"] == "POST"


def test_origin_check(client, token):
    assert post(client, PING, token=token, HTTP_ORIGIN="https://evil.example").status_code == 403
    assert post(client, PING, token=token, HTTP_ORIGIN="http://testserver").status_code == 200
    with override_settings(CSRF_TRUSTED_ORIGINS=["https://app.example"]):
        assert post(client, PING, token=token, HTTP_ORIGIN="https://app.example").status_code == 200


def test_csrf_not_required(token):
    from django.test import Client as HttpClient

    assert post(HttpClient(enforce_csrf_checks=True), PING, token=token).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_under_asgi(superuser):
    _, raw = ApiToken.objects.create_token(superuser, "asgi")
    Client.objects.create(name="Ahmed")

    async def go():
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_objects", "arguments": {"model": "testapp.client"}},
        }
        return await AsyncClient().post(
            "/mcp/",
            data=json.dumps(body),
            content_type="application/json",
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {raw}", "MCP-Protocol-Version": VERSION},
        )

    resp = async_to_sync(go)()
    assert resp.status_code == 200, resp.content
    out = json.loads(resp.content)["result"]["structuredContent"]
    assert out["count"] == 1 and out["rows"][0][1] == "Ahmed"


def test_without_sdk_tool_layer_imports_and_transports_explain():
    """Simulate a project without the [mcp] extra."""
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['mcp'] = None; sys.modules['mcp_types'] = None\n"
        "import django, os; os.environ['DJANGO_SETTINGS_MODULE'] = 'tests.settings'; django.setup()\n"
        "from admin_api.mcp import AdminMcpSite\n"
        "from django.core.exceptions import ImproperlyConfigured\n"
        "try:\n"
        "    import admin_api.mcp.views\n"
        "except ImproperlyConfigured as exc:\n"
        "    print('GUARD', exc)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert 'GUARD The MCP transports need the official MCP SDK: pip install "django-admin-api[mcp]"' in out
