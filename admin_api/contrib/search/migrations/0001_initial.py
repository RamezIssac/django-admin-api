import logging

import django.db.models.deletion
from django.db import DatabaseError, migrations, models, transaction

logger = logging.getLogger("admin_api.search")
GIN_INDEX = "admin_api_search_entry_norm_trgm"


def create_trigram_index(apps, schema_editor):
    """Postgres only: pg_trgm and a GIN index on norm. Skipped with a warning when the role may not
    create the extension; search then runs without the index and without the trigram pass."""
    if schema_editor.connection.vendor != "postgresql":
        return
    try:
        with transaction.atomic(using=schema_editor.connection.alias):
            schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except DatabaseError as exc:
        logger.warning("admin_api_search: could not create pg_trgm (%s); no trigram index", exc)
        return
    schema_editor.execute(
        f"CREATE INDEX IF NOT EXISTS {GIN_INDEX} ON admin_api_search_entry USING gin (norm gin_trgm_ops)"
    )


def drop_trigram_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX IF EXISTS {GIN_INDEX}")


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="SearchEntry",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("kind", models.CharField(max_length=8)),
                ("object_pk", models.CharField(blank=True, max_length=255, null=True)),
                ("field", models.CharField(max_length=100)),
                ("language", models.CharField(blank=True, default="", max_length=15)),
                ("text", models.TextField()),
                ("norm", models.TextField()),
                ("updated_at", models.DateTimeField()),
                ("indexed_at", models.DateTimeField()),
                (
                    "content_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "search entries",
                "db_table": "admin_api_search_entry",
                "indexes": [models.Index(fields=["content_type", "object_pk"], name="admin_api_search_entry_obj")],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("content_type", "object_pk", "field", "language"),
                        name="admin_api_search_entry_unique",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="SearchIndexState",
            fields=[
                ("label", models.CharField(max_length=100, primary_key=True, serialize=False)),
                ("fingerprint", models.CharField(max_length=64)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "admin_api_search_state",
            },
        ),
        migrations.RunPython(create_trigram_index, drop_trigram_index),
    ]
