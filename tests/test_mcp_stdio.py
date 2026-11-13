"""stdio transport: the server driven in-process by the official MCP client, and mcp_serve's CLI."""

from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.core.management import CommandError, call_command
from django.test import override_settings
from mcp.client import Client

from admin_api.mcp.server import build_server
from admin_api.mcp.stdio import StdioRequestFactory
from admin_api.models import ApiToken
from tests.testapp.models import Client as Customer
from tests.testapp.models import Sales
from tests.urls import mcp_site


def run(server, steps, mode="legacy"):
    """Connect the official client to ``server`` in-process and run ``steps(client)``."""

    async def go():
        async with Client(server, mode=mode) as client:
            return await steps(client)

    return async_to_sync(go)()


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_stdio_server_in_process(superuser, mode):
    _, raw = ApiToken.objects.create_token(superuser, "stdio", kind="mcp")
    ahmed = Customer.objects.create(name="Ahmed")
    server = build_server(mcp_site, StdioRequestFactory(mcp_site, raw_token=raw))

    async def steps(client):
        tools = await client.list_tools()
        listed = await client.call_tool("list_models", {})
        dry = await client.call_tool(
            "create_object", {"model": "testapp.sales", "data": {"slug": "S-1", "client": ahmed.pk}, "dry_run": True}
        )
        real = await client.call_tool(
            "create_object", {"model": "testapp.sales", "data": {"slug": "S-1", "client": ahmed.pk}}
        )
        return tools, listed, dry, real

    tools, listed, dry, real = run(server, steps, mode)
    assert len(tools.tools) == 8
    assert "testapp.sales" in {m["model"] for m in listed.structured_content["models"]}
    assert dry.structured_content["valid"] is True and not dry.is_error
    assert real.structured_content["object"]["slug"] == "S-1"
    assert Sales.objects.get().value == Decimal(0)


def test_token_checked_on_every_call(superuser):
    token, raw = ApiToken.objects.create_token(superuser, "stdio", kind="mcp", scopes=["testapp.client:view"])
    server = build_server(mcp_site, StdioRequestFactory(mcp_site, raw_token=raw))

    async def steps(client):
        before = await client.call_tool("list_models", {})
        await sync_to_async(token.revoke)()
        after = await client.call_tool("list_models", {})
        return before, after

    before, after = run(server, steps)
    assert [m["model"] for m in before.structured_content["models"]] == ["testapp.client"]  # scopes apply
    assert after.is_error and after.structured_content["error"] == "permission_denied"


def test_debug_user_mode(superuser):
    server = build_server(mcp_site, StdioRequestFactory(mcp_site, user=superuser))

    async def steps(client):
        return await client.call_tool("list_models", {})

    assert not run(server, steps).is_error


# --------------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------------
SITE = ["--site", "tests.urls.mcp_site"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "args, message",
    [
        (SITE, "Give --token"),
        ([*SITE, "--token", "aapi_dead_beef"], "Invalid, revoked or expired token."),
        (["--site", "tests.urls.nope"], "Cannot import"),
        (["--site", "tests.urls.api_site"], "is not an AdminMcpSite"),
    ],
)
def test_command_errors(args, message, monkeypatch):
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    with pytest.raises(CommandError, match=message):
        call_command("mcp_serve", *args)


@override_settings(DEBUG=True)
def test_command_unknown_user(db):
    with pytest.raises(CommandError, match="No user 'nobody'."):
        call_command("mcp_serve", *SITE, "--user", "nobody")


def test_command_user_needs_debug(superuser):
    with override_settings(DEBUG=False), pytest.raises(CommandError, match="only allowed with DEBUG=True"):
        call_command("mcp_serve", *SITE, "--user", "root")


def test_command_token_and_user_exclusive(superuser):
    _, raw = ApiToken.objects.create_token(superuser, "t")
    with pytest.raises(CommandError, match="either a token or --user"):
        call_command("mcp_serve", *SITE, "--token", raw, "--user", "root")


def test_command_serves(superuser, monkeypatch):
    """With valid arguments the command hands the site and a token-backed factory to serve()."""
    _, raw = ApiToken.objects.create_token(superuser, "t")
    seen = {}
    monkeypatch.setattr("admin_api.mcp.stdio.serve", lambda site, factory: seen.update(site=site, factory=factory))
    monkeypatch.setenv("ADMIN_API_TOKEN", raw)
    call_command("mcp_serve", *SITE)
    assert seen["site"] is mcp_site
    request = seen["factory"]()
    assert request.user == superuser and request.is_mcp and request.api_token.prefix == raw.split("_")[1]


def test_default_site_discovery(monkeypatch):
    from admin_api.management.commands.mcp_serve import Command

    monkeypatch.setattr("admin_api.mcp.sites.all_mcp_sites", lambda: [mcp_site])
    assert Command().get_site(None) is mcp_site
    monkeypatch.setattr("admin_api.mcp.sites.all_mcp_sites", lambda: [mcp_site, mcp_site])
    with pytest.raises(CommandError, match="Several AdminMcpSites"):
        Command().get_site(None)
    monkeypatch.setattr("admin_api.mcp.sites.all_mcp_sites", list)
    with pytest.raises(CommandError, match="No AdminMcpSite found"):
        Command().get_site(None)
