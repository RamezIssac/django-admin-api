from __future__ import annotations

from django.contrib import admin

API_DISPLAY_ATTR = "admin_api_display"


def display(function=None, *, boolean=None, ordering=None, description=None, empty_value=None):
    """Like ``@admin.display`` and also marks the callable as safe to expose in the API.

    Callables in ``list_display`` are left out of the API unless they are listed in
    ``api_list_display`` or decorated with this.
    """

    def decorator(func):
        func = admin.display(boolean=boolean, ordering=ordering, description=description, empty_value=empty_value)(func)
        setattr(func, API_DISPLAY_ATTR, True)
        return func

    if function is None:
        return decorator
    return decorator(function)


def is_api_display(func) -> bool:
    return bool(getattr(func, API_DISPLAY_ATTR, False))
