"""Per-user MCP memory: audience, language and notes feed the instructions and translation hooks."""

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone, translation

from admin_api import AdminApiSite
from admin_api.admin import ApiTokenAdmin, McpUserMemoryAdmin
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.sites import BUSINESS_WORDING, DEFAULT_INSTRUCTIONS, DEVELOPER_WORDING
from admin_api.mcp.views import make_view
from admin_api.memory import UserMemory, load_user_memory, set_preference
from admin_api.models import ApiToken, McpUserMemory
from tests.testapp.admin import site as admin_site
from tests.testapp.models import Client, Sales
from tests.urls import api_site


@pytest.fixture
def mcp():
    return AdminMcpSite(api_site)


@pytest.fixture
def call(rf, superuser, mcp):
    def do(name, args=None, *, user=superuser, site=mcp):
        request = rf.post("/mcp/")
        request.user = user
        result = site.call_tool(request, name, args or {})
        return result.data, result.is_error

    return do


@pytest.fixture
def sale(db):
    return Sales.objects.create(slug="S-1", client=Client.objects.create(name="Ahmed"), value=Decimal(100))


def remember(user, key, value):
    return McpUserMemory.objects.create(user=user, kind=McpUserMemory.PREFERENCE, key=key, value=value)


def instructions_for(rf, mcp, user):
    request = rf.post("/mcp/")
    request.user = user
    return mcp.get_instructions(mcp.prepare_request(request))


# --------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------
def test_no_rows_keeps_phase_one_behaviour(rf, mcp, superuser):
    request = mcp.prepare_request(rf.post("/mcp/"))
    request.user = superuser
    del request.mcp_memory
    assert mcp.get_user_memory(request) == UserMemory(audience="business", language=None, notes="")
    assert mcp.get_instructions(request) == DEFAULT_INSTRUCTIONS
    assert mcp.get_language(request) is None


def test_memory_is_loaded_once_per_request(rf, mcp, superuser, django_assert_num_queries):
    remember(superuser, "language", "ar")
    request = rf.post("/mcp/")
    request.user = superuser
    with django_assert_num_queries(1):
        mcp.prepare_request(request)
        mcp.get_instructions(request)
        mcp.get_language(request)


def test_anonymous_user_gets_defaults_without_a_query(rf, mcp, django_assert_num_queries, db):
    from django.contrib.auth.models import AnonymousUser

    request = rf.post("/mcp/")
    request.user = AnonymousUser()
    with django_assert_num_queries(0):
        assert mcp.prepare_request(request).mcp_memory == UserMemory()


def test_invalid_stored_value_falls_back_to_default(superuser):
    remember(superuser, "audience", "wizard")
    remember(superuser, "language", "xx")
    assert load_user_memory(superuser) == UserMemory()


# --------------------------------------------------------------------------------------------
# instructions
# --------------------------------------------------------------------------------------------
def test_developer_audience_swaps_the_wording_paragraph(rf, mcp, superuser):
    remember(superuser, "audience", "developer")
    text = instructions_for(rf, mcp, superuser)
    assert BUSINESS_WORDING not in text
    assert DEVELOPER_WORDING in text
    assert text == DEFAULT_INSTRUCTIONS.replace(BUSINESS_WORDING, DEVELOPER_WORDING)


def test_language_and_notes_are_appended(rf, mcp, superuser):
    remember(superuser, "language", "ar")
    remember(superuser, "notes", "I run the Cairo branch.")
    text = instructions_for(rf, mcp, superuser)
    assert text.startswith(DEFAULT_INSTRUCTIONS)
    assert text.endswith("\nAnswer the user in Arabic.\nThe user's own notes for you: I run the Cairo branch.")


def test_custom_instructions_only_get_the_appended_lines(rf, superuser):
    remember(superuser, "audience", "developer")
    remember(superuser, "language", "de")
    mcp = AdminMcpSite(api_site, instructions="Site rules.")
    assert instructions_for(rf, mcp, superuser) == "Site rules.\nAnswer the user in German."


def test_initialize_sends_the_users_instructions(rf, superuser):
    remember(superuser, "audience", "developer")
    remember(superuser, "language", "ar")
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
    response = make_view(AdminMcpSite(api_site, name="memory_init"))(request)
    instructions = json.loads(response.content)["result"]["instructions"]
    assert DEVELOPER_WORDING in instructions
    assert instructions.endswith("Answer the user in Arabic.")


# --------------------------------------------------------------------------------------------
# language
# --------------------------------------------------------------------------------------------
def test_language_preference_translates_tool_calls(call, superuser, sale):
    remember(superuser, "language", "de")
    out, err = call("update_object", {"model": "testapp.sales", "id": sale.pk, "data": {"value": "x"}})
    assert err and out["fields"]["value"] == ["Bitte eine Zahl eingeben."]
    assert translation.get_language() != "de"


# --------------------------------------------------------------------------------------------
# set_preference
# --------------------------------------------------------------------------------------------
def test_set_preference_stores_and_applies_to_the_next_call(call, superuser, sale):
    out, err = call("set_preference", {"key": "language", "value": "de"})
    assert not err, out
    assert out["preferences"] == {"audience": "business", "language": "de", "notes": ""}
    assert McpUserMemory.objects.get(user=superuser, key="language").value == "de"
    out, err = call("update_object", {"model": "testapp.sales", "id": sale.pk, "data": {"value": "x"}})
    assert err and out["fields"]["value"] == ["Bitte eine Zahl eingeben."]


def test_set_preference_clears_with_empty_value(call, superuser):
    remember(superuser, "audience", "developer")
    out, err = call("set_preference", {"key": "audience", "value": ""})
    assert not err and out["preferences"]["audience"] == "business"
    assert not McpUserMemory.objects.filter(user=superuser).exists()


@pytest.mark.parametrize(
    "args, detail",
    [
        ({"key": "colour", "value": "red"}, "Unknown preference. Use one of: audience, language, notes."),
        ({"key": "audience", "value": "admin"}, "Audience is one of: business, developer."),
        ({"key": "language", "value": "xx"}, "Language is one of: en, ar, de."),
        ({"key": "notes", "value": "x" * 501}, "Notes are at most 500 characters."),
        ({"key": "notes", "value": 5}, "The value must be text."),
    ],
)
@override_settings(LANGUAGES=[("en", "English"), ("ar", "Arabic"), ("de", "German")])
def test_set_preference_validates(call, superuser, args, detail):
    out, err = call("set_preference", args)
    assert err and out["error"] == "invalid_arguments" and out["detail"] == detail
    assert not McpUserMemory.objects.exists()


def test_set_preference_only_touches_the_callers_rows(call, superuser):
    other = get_user_model().objects.create_superuser("other", "o@x.io", "pw")
    remember(other, "language", "ar")
    call("set_preference", {"key": "language", "value": "de"})
    assert McpUserMemory.objects.get(user=other).value == "ar"
    assert McpUserMemory.objects.get(user=superuser).value == "de"


def test_set_preference_is_listed_on_read_only_sites():
    tool = AdminMcpSite(api_site, read_only=True).get_tool("set_preference")
    assert tool is not None and not tool.writes


def test_audit_log_leaves_the_value_out(call, caplog):
    with caplog.at_level("INFO", logger="admin_api.mcp.audit"):
        call("set_preference", {"key": "notes", "value": "private note"})
    assert "set_preference" in caplog.text and "private note" not in caplog.text


# --------------------------------------------------------------------------------------------
# exposure and admin
# --------------------------------------------------------------------------------------------
def test_memory_is_not_exposed_through_mcp(call, superuser):
    row = remember(superuser, "notes", "secret")
    out, _ = call("list_models")
    assert "admin_api.mcpusermemory" not in {m["model"] for m in out["models"]}
    out, err = call("get_object", {"model": "admin_api.mcpusermemory", "id": row.pk})
    assert err and out["detail"] == "This kind of record is not available here."


def test_admin_shows_own_rows_and_validates_preferences(rf, superuser):
    clerk = get_user_model().objects.create_user("clerk", "c@x.io", "pw", is_staff=True)
    remember(superuser, "notes", "root's")
    mine = remember(clerk, "notes", "mine")
    model_admin = admin_site._registry[McpUserMemory]
    assert isinstance(model_admin, McpUserMemoryAdmin)
    request = rf.get("/")
    request.user = clerk
    assert list(model_admin.get_queryset(request)) == [mine]
    assert "user" not in model_admin.get_fields(request)

    form_class = model_admin.get_form(request)
    form = form_class(data={"kind": "preference", "key": "audience", "value": '"wizard"'})
    assert not form.is_valid()
    assert "Audience is one of: business, developer." in str(form.errors)
    form = form_class(data={"kind": "preference", "key": "audience", "value": '"developer"'})
    assert form.is_valid(), form.errors


def test_memory_rows_go_with_the_user(superuser):
    remember(superuser, "notes", "bye")
    superuser.delete()
    assert not McpUserMemory.objects.exists()


# --------------------------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------------------------
def test_prune_deletes_old_picks_and_expired_rows(superuser):
    now = timezone.now()
    old_pick = McpUserMemory.objects.create(user=superuser, kind="picked", key="ahmed", value={"id": 1})
    new_pick = McpUserMemory.objects.create(user=superuser, kind="picked", key="sara", value={"id": 2})
    expired = remember(superuser, "notes", "temporary")
    kept = remember(superuser, "language", "ar")
    McpUserMemory.objects.filter(pk=old_pick.pk).update(updated=now - timedelta(days=181))
    McpUserMemory.objects.filter(pk=kept.pk).update(updated=now - timedelta(days=400))
    McpUserMemory.objects.filter(pk=expired.pk).update(expires_at=now - timedelta(minutes=1))

    out = StringIO()
    call_command("prune_mcp_memory", "--dry-run", stdout=out)
    assert out.getvalue().strip() == "2 memory rows would be deleted."
    assert McpUserMemory.objects.count() == 4

    out = StringIO()
    call_command("prune_mcp_memory", "--batch-size", "1", stdout=out)
    assert out.getvalue().splitlines() == ["deleted 1/2", "deleted 2/2", "2 memory rows deleted."]
    assert set(McpUserMemory.objects.values_list("pk", flat=True)) == {new_pick.pk, kept.pk}


# --------------------------------------------------------------------------------------------
# rows each query reads and writes
# --------------------------------------------------------------------------------------------
def test_loading_reads_only_the_users_preference_rows(superuser):
    other = get_user_model().objects.create_superuser("other", "o@x.io", "pw")
    remember(other, "language", "ar")
    McpUserMemory.objects.create(user=superuser, kind=McpUserMemory.PICKED, key="notes", value="a pick")
    assert load_user_memory(superuser) == UserMemory()


def test_an_invalid_row_does_not_hide_the_rows_after_it(superuser):
    remember(superuser, "audience", "wizard")
    remember(superuser, "language", "de")
    assert load_user_memory(superuser) == UserMemory(language="de")


def test_setting_and_clearing_touch_only_that_preference_row(superuser):
    other = get_user_model().objects.create_superuser("other", "o@x.io", "pw")
    remember(other, "audience", "developer")
    remember(superuser, "audience", "developer")
    remember(superuser, "language", "de")
    pick = McpUserMemory.objects.create(user=superuser, kind=McpUserMemory.PICKED, key="audience", value="a pick")
    set_preference(superuser, "audience", None)
    set_preference(superuser, "notes", "x" * 500)  # the limit itself is allowed
    rows = McpUserMemory.objects.filter(user=superuser, kind=McpUserMemory.PREFERENCE)
    assert sorted(rows.values_list("key", flat=True)) == ["language", "notes"]
    assert McpUserMemory.objects.get(user=other).value == "developer"
    pick.refresh_from_db()
    assert pick.value == "a pick"
    set_preference(superuser, "audience", "developer")
    pick.refresh_from_db()
    assert pick.value == "a pick" and rows.get(key="audience").value == "developer"


def test_set_preference_checks_the_key_type_and_says_when_it_applies(call):
    out, err = call("set_preference", {"key": 5, "value": "x"})
    assert err and out["detail"] == "'key' has the wrong type."
    out, err = call("set_preference", {"key": "audience", "value": "developer"})
    assert not err
    assert out["note"] == "Language applies from the next tool call; audience and notes from the next connection."


def test_admin_superuser_sees_and_assigns_every_users_rows(rf, superuser, django_assert_num_queries):
    clerk = get_user_model().objects.create_user("clerk", "c@x.io", "pw", is_staff=True)
    rows = [remember(superuser, "notes", "root's"), remember(clerk, "notes", "clerk's")]
    model_admin = admin_site._registry[McpUserMemory]
    request = rf.get("/")
    request.user = superuser
    with django_assert_num_queries(1):
        listed = sorted((r.pk, r.user.username) for r in model_admin.get_queryset(request))
    assert listed == [(rows[0].pk, "root"), (rows[1].pk, "clerk")]
    assert model_admin.get_fields(request) == ["user", "kind", "key", "value", "expires_at"]

    assigned = McpUserMemory(user=clerk, kind="preference", key="language", value="de")
    model_admin.save_model(request, assigned, None, False)
    unassigned = McpUserMemory(kind="preference", key="audience", value="developer")
    model_admin.save_model(request, unassigned, None, False)
    request.user = clerk
    forged = McpUserMemory(user=superuser, kind="preference", key="audience", value="developer")
    model_admin.save_model(request, forged, None, False)
    assert (assigned.user, unassigned.user, forged.user) == (clerk, superuser, clerk)


def test_admin_form_cleans_preferences_only(rf, superuser):
    request = rf.get("/")
    request.user = superuser
    form_class = admin_site._registry[McpUserMemory].get_form(request)
    form = form_class(data={"user": superuser.pk, "kind": "preference", "key": "notes", "value": '"  hi  "'})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["value"] == "hi"
    pick = {"user": superuser.pk, "kind": "picked", "key": "ahmed", "value": '{"id": 1}'}
    assert form_class(data=pick).is_valid()


def test_api_site_registers_its_admins_once():
    fresh = admin.AdminSite(name="memory_fresh")
    AdminApiSite(fresh, name="memory_fresh_api")
    assert isinstance(fresh._registry[McpUserMemory], McpUserMemoryAdmin)
    assert isinstance(fresh._registry[ApiToken], ApiTokenAdmin)

    class OwnMemoryAdmin(admin.ModelAdmin):
        pass

    own = admin.AdminSite(name="memory_own")
    own.register(McpUserMemory, OwnMemoryAdmin)
    AdminApiSite(own, name="memory_own_api")
    assert type(own._registry[McpUserMemory]) is OwnMemoryAdmin


def test_prune_days_default_and_option(superuser):
    now = timezone.now()
    pick = McpUserMemory.objects.create(user=superuser, kind="picked", key="ahmed", value={"id": 1})
    McpUserMemory.objects.filter(pk=pick.pk).update(updated=now - timedelta(days=180, hours=1))
    out = StringIO()
    call_command("prune_mcp_memory", "--dry-run", stdout=out)
    assert out.getvalue().strip() == "1 memory rows would be deleted."
    out = StringIO()
    call_command("prune_mcp_memory", "--dry-run", "--days", "200", stdout=out)
    assert out.getvalue().strip() == "0 memory rows would be deleted."
