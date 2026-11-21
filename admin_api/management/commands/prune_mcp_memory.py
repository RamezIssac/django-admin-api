"""Delete expired MCP user memory: ``picked`` rows unused for ``--days`` and any row past ``expires_at``."""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from admin_api.models import McpUserMemory


class Command(BaseCommand):
    help = "Delete picked-record memory not used for --days, and every memory row past its expiry."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=180, help="Keep picked rows used within this many days.")
        parser.add_argument("--batch-size", type=int, default=1000, help="Rows deleted per batch.")
        parser.add_argument("--dry-run", action="store_true", help="Count the rows, delete nothing.")

    def handle(self, *args, days, batch_size, dry_run, **options):
        now = timezone.now()
        stale = Q(kind=McpUserMemory.PICKED, updated__lt=now - timedelta(days=days)) | Q(expires_at__lte=now)
        rows = McpUserMemory.objects.filter(stale)
        total = rows.count()
        if dry_run:
            self.stdout.write(f"{total} memory rows would be deleted.")
            return
        deleted = 0
        while ids := list(rows.order_by("pk").values_list("pk", flat=True)[:batch_size]):
            McpUserMemory.objects.filter(pk__in=ids).delete()  # autocommit: each batch is durable
            deleted += len(ids)
            self.stdout.write(f"deleted {deleted}/{total}")
        self.stdout.write(self.style.SUCCESS(f"{deleted} memory rows deleted."))
