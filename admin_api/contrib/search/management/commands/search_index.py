from django.core.management.base import BaseCommand, CommandError

from ...conf import get_engine
from ...indexer import get_indexer


class Command(BaseCommand):
    help = (
        "search_index ensure: refresh the model label rows and rebuild the models whose indexed fields, "
        "languages or engine changed since the last run. Idempotent; run it after migrate on every deploy."
    )

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        ensure = sub.add_parser("ensure", help="Refresh label rows; rebuild changed models.")
        ensure.add_argument("--batch-size", type=int, default=1000, help="Objects per transaction (default 1000).")

    def handle(self, *args, action=None, batch_size=1000, **options):
        problems = get_engine().check()
        if problems:
            raise CommandError(" ".join(f"{p.msg} {p.hint}" for p in problems))
        indexer = get_indexer()
        self.stdout.write(f"model rows: {indexer.refresh_model_rows()}")
        rebuilt = indexer.rebuild(batch_size=batch_size, force=False, stdout=self.stdout)
        self.stdout.write(f"rebuilt: {', '.join(rebuilt) or 'nothing'}")
