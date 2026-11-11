import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from admin_api.auth import get_token_model


class Command(BaseCommand):
    help = "Create an API token for a user and print it (shown only once)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", default="cli")
        parser.add_argument("--kind", default="pat", choices=["pat", "mcp", "service"])
        parser.add_argument("--expires-days", type=int, default=None)
        parser.add_argument(
            "--scope",
            action="append",
            default=[],
            dest="scopes",
            help="Narrow the token, e.g. --scope store.sales:view --scope 'cash.*:*'. Repeatable.",
        )

    def handle(self, username, name, kind, expires_days, scopes, **options):
        User = get_user_model()
        try:
            user = User.objects.get(**{User.USERNAME_FIELD: username})
        except User.DoesNotExist as exc:
            raise CommandError(f"No user {username!r}.") from exc
        expires_at = timezone.now() + datetime.timedelta(days=expires_days) if expires_days else None
        try:
            _token, raw = get_token_model().objects.create_token(
                user, name, expires_at=expires_at, kind=kind, scopes=scopes
            )
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        self.stdout.write(raw)
