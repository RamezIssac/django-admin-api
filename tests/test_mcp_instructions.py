"""Per-user MCP instructions: an admin-set text added to the server instructions at initialize."""

import json

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from admin_api import AdminApiSite
from admin_api.admin import ApiTokenAdmin, McpUserInstructionsAdmin
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.sites import DEFAULT_INSTRUCTIONS, TRIMMED_NOTE, USER_INSTRUCTIONS_HEADING
from admin_api.mcp.views import make_view
from admin_api.models import ApiToken, McpUserInstructions
from tests.testapp.admin import site as admin_site
from tests.urls import api_site


@pytest.fixture
def mcp():
    return AdminMcpSite(api_site)


def instructions_for(rf, mcp, user):
    request = rf.post("/mcp/")
    request.user = user
    return mcp.get_instructions(request)


def instruct(user, text):
    return McpUserInstructions.objects.create(user=user, instructions=text)


def test_no_row_sends_the_site_instructions(rf, mcp, superuser, django_assert_num_queries):
    with django_assert_num_queries(1):
        assert instructions_for(rf, mcp, superuser) == DEFAULT_INSTRUCTIONS


def test_anonymous_user_gets_the_site_instructions_without_a_query(rf, mcp, db, django_assert_num_queries):
    with django_assert_num_queries(0):
        assert instructions_for(rf, mcp, AnonymousUser()) == DEFAULT_INSTRUCTIONS


def test_the_users_instructions_follow_the_site_instructions(rf, superuser):
    instruct(superuser, "Answer in Arabic. I am a developer: model paths and field names are fine.")
    other = get_user_model().objects.create_user("other", "o@x.io", "pw")
    instruct(other, "Answer in German.")
    mcp = AdminMcpSite(api_site, instructions="Site rules.")
    assert instructions_for(rf, mcp, superuser) == (
        "Site rules.\nInstructions for this user (they win where they differ from the above):\n"
        "Answer in Arabic. I am a developer: model paths and field names are fine."
    )


def test_instructions_at_the_limit_are_sent_whole(rf, superuser):
    mcp = AdminMcpSite(api_site, instructions="Site rules.")
    head = f"Site rules.\n{USER_INSTRUCTIONS_HEADING}\n"
    instruct(superuser, "x" * (4096 - len(head)))
    assert instructions_for(rf, mcp, superuser) == head + "x" * (4096 - len(head))


def test_longer_instructions_are_cut_with_a_note(rf, superuser):
    mcp = AdminMcpSite(api_site, instructions="Site rules.")
    head = f"Site rules.\n{USER_INSTRUCTIONS_HEADING}\n"
    instruct(superuser, "x" * (4097 - len(head)))
    text = instructions_for(rf, mcp, superuser)
    assert len(text) == 4096
    assert text == head + "x" * (4096 - len(head) - len(TRIMMED_NOTE)) + TRIMMED_NOTE
    assert TRIMMED_NOTE == "\n(Cut here: the instructions are longer than the client accepts.)"


def test_initialize_sends_the_users_instructions(rf, superuser):
    instruct(superuser, "Answer in Arabic.")
    raw = ApiToken.objects.create_token(superuser, "mcp", kind="mcp")[1]
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    request = rf.post(
        "/mcp/",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_ACCEPT="application/json, text/event-stream",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    response = make_view(AdminMcpSite(api_site, name="instructions_init"))(request)
    instructions = json.loads(response.content)["result"]["instructions"]
    assert instructions == f"{DEFAULT_INSTRUCTIONS}\n{USER_INSTRUCTIONS_HEADING}\nAnswer in Arabic."


def test_instructions_are_not_exposed_through_mcp(rf, mcp, superuser):
    row = instruct(superuser, "secret")
    request = rf.post("/mcp/")
    request.user = superuser
    out = mcp.call_tool(request, "list_models", {}).data
    assert "admin_api.mcpuserinstructions" not in {m["model"] for m in out["models"]}
    result = mcp.call_tool(request, "get_object", {"model": "admin_api.mcpuserinstructions", "id": row.pk})
    assert result.is_error and result.data["detail"] == "This kind of record is not available here."


def test_instructions_go_with_the_user(superuser):
    instruct(superuser, "bye")
    superuser.delete()
    assert not McpUserInstructions.objects.exists()


def test_api_site_registers_its_admins_once():
    assert isinstance(admin_site._registry[McpUserInstructions], McpUserInstructionsAdmin)
    fresh = admin.AdminSite(name="instructions_fresh")
    AdminApiSite(fresh, name="instructions_fresh_api")
    assert isinstance(fresh._registry[McpUserInstructions], McpUserInstructionsAdmin)
    assert isinstance(fresh._registry[ApiToken], ApiTokenAdmin)

    class OwnInstructionsAdmin(admin.ModelAdmin):
        pass

    own = admin.AdminSite(name="instructions_own")
    own.register(McpUserInstructions, OwnInstructionsAdmin)
    AdminApiSite(own, name="instructions_own_api")
    assert type(own._registry[McpUserInstructions]) is OwnInstructionsAdmin
