from django.core import checks


@checks.register(checks.Tags.database)
def check_engine(app_configs=None, databases=None, **kwargs):
    """The configured engine's own checks (``db``: the database ctype). Runs with ``--database``
    and before ``migrate``."""
    from .conf import get_engine

    if not databases or "default" not in databases:
        return []
    return get_engine().check()
