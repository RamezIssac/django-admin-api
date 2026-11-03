"""Twin lookup order: get_api_X(request) → api_X → get_X(request) → X."""

from django.contrib import admin

from admin_api import ApiAdminMixin, api_value, resolve
from tests.testapp.models import Client


def make(cls_body, mixin=False):
    bases = (ApiAdminMixin, admin.ModelAdmin) if mixin else (admin.ModelAdmin,)
    cls = type("A", bases, cls_body)
    return cls(Client, admin.AdminSite(name="tmp"))


class TestLookupOrder:
    def test_level4_plain_attribute(self, api_request):
        ma = make({"list_display": ["name"]})
        assert resolve(ma, "list_display", api_request) == ["name"]

    def test_level3_normal_getter(self, api_request):
        ma = make({"list_display": ["name"], "get_list_display": lambda self, r: ["from_get"]})
        assert resolve(ma, "list_display", api_request) == ["from_get"]

    def test_level2_api_attribute_beats_getter(self, api_request):
        ma = make({"api_list_display": ["api_attr"], "get_list_display": lambda self, r: ["from_get"]})
        assert resolve(ma, "list_display", api_request) == ["api_attr"]

    def test_level1_api_method_beats_everything(self, api_request):
        ma = make({"api_list_display": ["api_attr"], "get_api_list_display": lambda self, r: ["api_method"]})
        assert resolve(ma, "list_display", api_request) == ["api_method"]

    def test_api_attribute_none_falls_through(self, api_request):
        ma = make({"list_display": ["name"], "api_list_display": None})
        assert resolve(ma, "list_display", api_request) == ["name"]

    def test_hook_without_normal_getter(self, api_request):
        ma = make({"list_per_page": 20, "api_list_per_page": 5})
        assert resolve(ma, "list_per_page", api_request) == 5

    def test_new_hook_without_normal_counterpart(self, api_request):
        ma = make({})
        assert resolve(ma, "detail_fields", api_request) is None
        ma = make({"api_detail_fields": ["name"]})
        assert resolve(ma, "detail_fields", api_request) == ["name"]


class TestPlainAdmin:
    def test_api_value_resolves_twin(self, sales_admin, api_request):
        assert api_value(sales_admin, "list_display", api_request) == ["slug", "value"]

    def test_html_unchanged(self, sales_admin, html_request):
        assert sales_admin.get_list_display(html_request) == ["slug", "client", "value"]


class TestMixinRouting:
    def test_getter_routes_on_api(self, client_admin, api_request, html_request):
        assert client_admin.get_list_display(api_request) == ["name", "notes_length"]
        assert client_admin.get_list_display(html_request) == ["name", "badge"]

    def test_readonly_twin(self, client_admin, api_request, html_request):
        assert client_admin.get_readonly_fields(api_request) == ["notes"]
        assert client_admin.get_readonly_fields(html_request) == []

    def test_internal_django_calls_are_api_aware(self, client_admin, api_request, html_request):
        # get_form() internally consults get_readonly_fields(); the API twin must apply there.
        assert "notes" not in client_admin.get_form(api_request).base_fields
        assert "notes" in client_admin.get_form(html_request).base_fields

    def test_get_api_queryset_calling_super_does_not_recurse(self, db, client_admin, api_request, html_request):
        Client.objects.create(name="Ahmed")
        Client.objects.create(name="Bassem")
        assert [c.name for c in client_admin.get_queryset(api_request)] == ["Ahmed"]
        assert client_admin.get_queryset(html_request).count() == 2

    def test_user_override_calling_super_keeps_routing(self, api_request, html_request):
        class Admin(ApiAdminMixin, admin.ModelAdmin):
            api_list_display = ["api"]

            def get_list_display(self, request):
                return [*super().get_list_display(request), "extra"]

        ma = Admin(Client, admin.AdminSite(name="tmp2"))
        assert api_value(ma, "list_display", api_request) == ["api", "extra"]
        assert ma.get_list_display(html_request) == ["__str__", "extra"]

    def test_api_form_is_used_as_base(self, api_request, html_request):
        from django import forms

        class ApiForm(forms.ModelForm):
            marker = True

        class Admin(ApiAdminMixin, admin.ModelAdmin):
            api_form = ApiForm

        ma = Admin(Client, admin.AdminSite(name="tmp3"))
        assert issubclass(ma.get_form(api_request), ApiForm)
        assert not issubclass(ma.get_form(html_request), ApiForm)
