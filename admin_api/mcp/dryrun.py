"""Mail capture for MCP dry runs: e-mail sent while a dry run runs is kept, not delivered.

Every e-mail backend opened inside :func:`capture_mail` (``send_mail``, ``mailers``, ``get_connection``
or a backend built by hand) collects its messages instead of sending them. The capture is per
context (a ``ContextVar``): a concurrent real request still sends. A connection opened before
the dry run still sends.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from django.core.mail.backends.base import BaseEmailBackend

_captured: ContextVar[list | None] = ContextVar("admin_api_dry_run_mail", default=None)
_original_init = BaseEmailBackend.__init__


def _init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    captured = _captured.get()
    if captured is not None:
        self.open = lambda: False
        self.close = lambda: None
        self.send_messages = lambda messages: captured.extend(messages) or len(messages)


@contextmanager
def capture_mail():
    """Yield a list that collects every message sent in this block instead of delivering it."""
    BaseEmailBackend.__init__ = _init
    messages: list = []
    reset = _captured.set(messages)
    try:
        yield messages
    finally:
        _captured.reset(reset)
