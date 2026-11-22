from django.core.management.base import BaseCommand, CommandError

from ...indexer import get_indexer


class Command(BaseCommand):
    help = "Rebuild the search index rows: object rows in batches (then prune), and the model label rows."

    def add_arguments(self, parser):
        parser.add_argument("--model", action="append", dest="models", metavar="APP.MODEL", help="Only this model.")
        parser.add_argument("--models-only", action="store_true", help="Only the model label rows.")
        parser.add_argument("--batch-size", type=int, default=1000, help="Objects per transaction (default 1000).")

    def handle(self, *args, models=None, models_only=False, batch_size=1000, **options):
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1.")
        indexer = get_indexer()
        labels = None
        if models:
            labels = {label.lower() for label in models}
            known = {m._meta.label_lower for m in indexer.admins}
            unknown = sorted(labels - known)
            if unknown:
                raise CommandError(f"Not indexed: {', '.join(unknown)}. Indexed: {', '.join(sorted(known)) or 'none'}.")
        rows = indexer.refresh_model_rows()
        self.stdout.write(f"model rows: {rows}")
        if not models_only:
            rebuilt = indexer.rebuild(labels, batch_size=batch_size, stdout=self.stdout)
            self.stdout.write(f"rebuilt: {', '.join(rebuilt) or 'nothing'}")
