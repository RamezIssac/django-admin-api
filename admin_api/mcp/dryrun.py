"""MCP dry runs: every e-mail backend built inside :func:`capture_mail` collects its messages instead
of sending them, and files stored inside :func:`delete_stored_files` are deleted after it, per context
(docs/DESIGN.md, dry runs)."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from django.core.files.storage import Storage
from django.core.mail.backends.base import BaseEmailBackend

_captured: ContextVar[list | None] = ContextVar("admin_api_dry_run_mail", default=None)
_stored: ContextVar[list | None] = ContextVar("admin_api_dry_run_files", default=None)
_original_init = BaseEmailBackend.__init__
_original_save = Storage.save


def _init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    captured = _captured.get()
    if captured is not None:
        self.open = lambda: False
        self.close = lambda: None
        self.send_messages = lambda messages: captured.extend(messages) or len(messages)


BaseEmailBackend.__init__ = _init


def _save(self, *args, **kwargs):
    name = _original_save(self, *args, **kwargs)
    stored = _stored.get()
    if stored is not None:
        stored.append((self, name))
    return name


Storage.save = _save


@contextmanager
def capture_mail():
    """Yield a list that collects every message sent in this block instead of delivering it."""
    messages: list = []
    reset = _captured.set(messages)
    try:
        yield messages
    finally:
        _captured.reset(reset)


@contextmanager
def delete_stored_files():
    """Delete the files stored in this block when it ends: a dry run rolls back the rows that name them."""
    stored: list = []
    reset = _stored.set(stored)
    try:
        yield
    finally:
        _stored.reset(reset)
        for storage, name in stored:
            storage.delete(name)
