"""Per-user MCP instructions: an admin-set text added to the server instructions at initialize."""

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from admin_api import AdminApiSite
from admin_api.admin import ApiTokenAdmin, McpUserInstructionsAdmin
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.sites import DEFAULT_INSTRUCTIONS, TRIMMED_NOTE, USER_INSTRUCTIONS_HEADING
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


@pytest.mark.parametrize("extra, cut", [(0, False), (1, True)])
def test_instructions_are_cut_past_the_limit_with_a_note(rf, superuser, extra, cut):
    mcp = AdminMcpSite(api_site, instructions="Site rules.")
    limit = mcp.instructions_max_length
    head = f"Site rules.\n{USER_INSTRUCTIONS_HEADING}\n"
    instruct(superuser, "x" * (limit + extra - len(head)))
    text = instructions_for(rf, mcp, superuser)
    assert len(text) == limit
    expected = (
        head + "x" * (limit - len(head) - len(TRIMMED_NOTE)) + TRIMMED_NOTE if cut else head + "x" * (limit - len(head))
    )
    assert text == expected


def test_long_site_instructions_are_cut_without_a_user_row(rf, superuser):
    mcp = AdminMcpSite(api_site, instructions="y" * 5000)
    assert instructions_for(rf, mcp, superuser) == "y" * (4096 - len(TRIMMED_NOTE)) + TRIMMED_NOTE


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
