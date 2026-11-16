"""Nested admins: ModelAdmins mounted inside a parent object (e.g. django-subadmin).

A nested admin is addressed by a **model path** plus the **parents** (one pk per level):

=========  ===================================================================
HTML       ``/admin/store/client/1/sales/7/salesitem/``
REST       ``/api/v1/store/client/1/sales/7/salesitem/``
MCP        ``model="store.client/sales/salesitem"``, ``parents=[1, 7]``
=========  ===================================================================

The REST path minus the pks is the model path; the pks in order are the parents. Each child
segment is the child's ``model_name``.

The core is generic. Libraries plug in through a :class:`NestedAdapter` listed in
``AdminApiSite.nested_adapters``; the django-subadmin adapter lives in
:mod:`admin_api.contrib.subadmin` and is enabled by default (duck-typed, no import).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ApiError


class BadModelPath(ApiError):
    status = 400
    code = "bad_model"


class BadParents(ApiError):
    status = 400
    code = "bad_parents"


@dataclass(frozen=True)
class ModelPath:
    """``app_label.model_name`` of a registered admin, then one segment per nesting level."""

    app_label: str
    model_name: str
    segments: tuple[str, ...] = ()

    @classmethod
    def parse(cls, label: str) -> ModelPath:
        """``"store.client/sales/salesitem"`` → ``ModelPath("store", "client", ("sales", "salesitem"))``."""
        root, *segments = (label or "").strip().lower().split("/")
        app_label, sep, model_name = root.partition(".")
        if not sep or not app_label or not model_name or not all(segments):
            raise BadModelPath(
                f"Expected 'app_label.model_name' (or a nested path like 'app.parent/child'), got {label!r}."
            )
        return cls(app_label, model_name, tuple(segments))

    @property
    def root_label(self) -> str:
        return f"{self.app_label}.{self.model_name}"

    @property
    def label(self) -> str:
        return "/".join((self.root_label, *self.segments))

    @property
    def depth(self) -> int:
        return len(self.segments)

    def child(self, segment: str) -> ModelPath:
        return ModelPath(self.app_label, self.model_name, (*self.segments, segment))

    def url(self, base: str, parents=(), *, quote=None) -> str:
        """REST url of the changelist, under ``base`` (e.g. ``/api/v1/``)."""
        if quote is None:
            from django.contrib.admin.utils import quote
        url = f"{base}{self.app_label}/{self.model_name}/"
        for pk, segment in zip(parents, self.segments):
            url += f"{quote(str(pk))}/{segment}/"
        return url

    def url_template(self, base: str) -> str:
        """Like :meth:`url` with ``{0}``, ``{1}``… in place of the parent pks."""
        return self.url(base, [f"{{{i}}}" for i in range(self.depth)], quote=str)


@dataclass
class Target:
    """What the current API call operates on. Set on ``request.admin_api_target`` by
    :func:`admin_api.services.get_model_admin`."""

    api_site: object
    path: ModelPath
    parents: list = field(default_factory=list)
    chain: list = field(default_factory=list)  # API admins, root first


class NestedAdapter:
    """How admin_api discovers and prepares the nested admins of one library.

    Subclass and add to ``AdminApiSite.nested_adapters``. All methods receive API admins
    (``ApiAdminMixin`` in the MRO) except :meth:`init_args`, which receives the raw child.
    """

    def get_children(self, model_admin) -> list:
        """The (raw) child admins mounted on ``model_admin``. ``[]`` when none or not handled."""
        return []

    def is_child(self, model_admin) -> bool:
        """Is ``model_admin`` a nested admin handled by this adapter?"""
        return False

    def segment(self, child) -> str:
        """The path segment of ``child`` under its parent."""
        return child.model._meta.model_name

    def init_args(self, child) -> tuple:
        """Constructor arguments to build an API twin of ``child``: ``type(child)(*init_args)``."""
        raise NotImplementedError

    def prepare_request(self, model_admin, request, parents: list) -> None:
        """Make ``request`` usable by the nested ``model_admin`` for the given parent pks
        (load and check every ancestor). Raise :class:`ApiError` (``NotFound``,
        ``PermissionDeniedError``) on failure."""
        raise NotImplementedError
