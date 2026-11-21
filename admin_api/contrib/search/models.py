from django.contrib.contenttypes.models import ContentType
from django.db import models


class SearchEntry(models.Model):
    """A row of the ``db`` engine: one object search field, or one model label in one language.
    ``updated_at`` moves when ``norm`` changes, ``indexed_at`` on every write (prune uses it)."""

    id = models.BigAutoField(primary_key=True)
    kind = models.CharField(max_length=8)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    object_pk = models.CharField(max_length=255, null=True, blank=True)
    field = models.CharField(max_length=100)
    language = models.CharField(max_length=15, blank=True, default="")
    text = models.TextField()
    norm = models.TextField()
    updated_at = models.DateTimeField()
    indexed_at = models.DateTimeField()

    class Meta:
        db_table = "admin_api_search_entry"
        verbose_name_plural = "search entries"
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_pk", "field", "language"], name="admin_api_search_entry_unique"
            )
        ]
        indexes = [models.Index(fields=["content_type", "object_pk"], name="admin_api_search_entry_obj")]

    def __str__(self):
        return self.text


class SearchIndexState(models.Model):
    """The fingerprint ``search_index ensure`` compares per indexed model."""

    label = models.CharField(max_length=100, primary_key=True)
    fingerprint = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_api_search_state"

    def __str__(self):
        return self.label
