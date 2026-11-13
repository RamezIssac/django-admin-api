"""Admin for API tokens. Registered on the wrapped site by ``AdminApiSite`` (not autodiscovered)."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _

from .scopes import normalize_scopes


class ApiTokenForm(forms.ModelForm):
    scopes_text = forms.CharField(
        label=_("scopes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "store.sales:view\nstore.*:view\ncash.*:*"}),
        help_text=_(
            "One per line: <app_label>.<model_name>:<view|add|change|delete>, '*' allowed in any part. "
            "Scopes only narrow the user's permissions. Leave empty for the user's full permissions."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["scopes_text"].initial = "\n".join(self.instance.scopes or [])

    def clean_scopes_text(self):
        return normalize_scopes(self.cleaned_data.get("scopes_text") or "")

    def save(self, commit=True):
        self.instance.scopes = self.cleaned_data.get("scopes_text") or []
        return super().save(commit)


class ApiTokenAdmin(admin.ModelAdmin):
    api_enabled = False  # never expose tokens through the API itself

    list_display = [
        "name",
        "user",
        "token_prefix",
        "kind",
        "scope_summary",
        "created",
        "expires_at",
        "last_used_at",
        "status",
    ]
    list_filter = ["kind"]
    search_fields = ["name", "prefix", "user__username"]
    actions = ["revoke_tokens"]
    form = ApiTokenForm

    @admin.display(description=_("token"))
    def token_prefix(self, obj):
        return f"aapi_{obj.prefix}_…"

    @admin.display(description=_("scopes"))
    def scope_summary(self, obj):
        return ", ".join(obj.scopes) if obj.scopes else _("full user permissions")

    @admin.display(description=_("status"))
    def status(self, obj):
        if obj.revoked_at:
            return _("revoked")
        if obj.is_expired:
            return _("expired")
        return _("active")

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("user")
        return qs if request.user.is_superuser else qs.filter(user=request.user)

    def get_fields(self, request, obj=None):
        fields = ["name", "kind", "expires_at", "scopes_text"]
        if request.user.is_superuser:
            fields.insert(0, "user")
        if obj is not None:
            fields += ["token_prefix", "created", "last_used_at", "revoked_at"]
        return fields

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        return ["user", "kind", "token_prefix", "created", "last_used_at", "revoked_at"]

    def save_model(self, request, obj, form, change):
        if not change:
            if not request.user.is_superuser or not obj.user_id:
                obj.user = request.user
            raw = obj.set_new_secret()
            super().save_model(request, obj, form, change)
            self.message_user(
                request,
                _("Token created. Copy it now, it will not be shown again: %(raw)s") % {"raw": raw},
                messages.WARNING,
            )
            return
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Revoke selected tokens"), permissions=["change"])
    def revoke_tokens(self, request, queryset):
        count = 0
        for token in queryset.filter(revoked_at__isnull=True):
            token.revoke()
            count += 1
        self.message_user(request, _("%(count)d token(s) revoked.") % {"count": count})
