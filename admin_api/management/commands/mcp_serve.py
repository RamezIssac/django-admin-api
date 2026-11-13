"""Serve an AdminMcpSite over stdio, for local MCP clients (Claude Code, Claude Desktop, ...).

    claude mcp add erp -e ADMIN_API_TOKEN=aapi_... -- python manage.py mcp_serve

Needs ``pip install "django-admin-api[mcp]"``. Logs go to stderr; stdout carries the protocol.
"""

import os
from importlib import import_module

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

TOKEN_ENV = "ADMIN_API_TOKEN"


class Command(BaseCommand):
    help = "Serve the admin as an MCP server over stdio."
    requires_system_checks = []  # checks print to stdout, which belongs to the protocol

    def add_arguments(self, parser):
        parser.add_argument(
            "--token",
            help=f"API token to act as (default: ${TOKEN_ENV}; the env var keeps it out of the process list).",
        )
        parser.add_argument("--user", help="Act as this user without a token. Only allowed with DEBUG=True.")
        parser.add_argument(
            "--site",
            help="Dotted path to the AdminMcpSite instance. Default: the only one defined in your URLconf.",
        )

    def handle(self, *args, token=None, user=None, site=None, **options):
        from admin_api.auth import get_token_model
        from admin_api.mcp.stdio import StdioRequestFactory, serve

        mcp_site = self.get_site(site)
        raw = token or os.environ.get(TOKEN_ENV)
        if raw and user:
            raise CommandError("Use either a token or --user, not both.")
        if raw:
            api_token = get_token_model().objects.get_for_raw(raw)
            if api_token is None or not api_token.user.is_active:
                raise CommandError("Invalid, revoked or expired token.")
            factory = StdioRequestFactory(mcp_site, raw_token=raw)
            who = f"{api_token.user} (token {api_token.prefix})"
        elif user:
            if not settings.DEBUG:
                raise CommandError("--user is only allowed with DEBUG=True. Use an API token.")
            User = get_user_model()
            try:
                account = User.objects.get(**{User.USERNAME_FIELD: user})
            except User.DoesNotExist as exc:
                raise CommandError(f"No user {user!r}.") from exc
            factory = StdioRequestFactory(mcp_site, user=account)
            who = f"{account} (no token, DEBUG)"
        else:
            raise CommandError(f"Give --token, set ${TOKEN_ENV}, or (DEBUG only) --user.")

        self.stderr.write(f"admin_api MCP server '{mcp_site.server_name}' on stdio as {who}")
        serve(mcp_site, factory)

    def get_site(self, dotted):
        from admin_api.mcp import AdminMcpSite
        from admin_api.mcp.sites import all_mcp_sites

        if dotted:
            try:
                site = import_string(dotted)
            except ImportError as exc:
                raise CommandError(f"Cannot import {dotted!r}: {exc}") from exc
            if not isinstance(site, AdminMcpSite):
                raise CommandError(f"{dotted!r} is not an AdminMcpSite.")
            return site
        import_module(settings.ROOT_URLCONF)  # sites are usually created in urls.py
        sites = all_mcp_sites()
        if len(sites) == 1:
            return sites[0]
        if not sites:
            raise CommandError("No AdminMcpSite found. Create one (see admin_api.mcp) or pass --site.")
        names = ", ".join(sorted(s.name for s in sites))
        raise CommandError(f"Several AdminMcpSites ({names}); pick one with --site dotted.path.")
