class ApiRequestMiddleware:
    """Sets ``request.is_api``, ``request.is_mcp`` and ``request.is_dry_run`` to ``False`` on
    every request.

    API views flip ``is_api`` to ``True``; MCP requests set ``is_api`` and ``is_mcp``; MCP
    ``dry_run`` writes set ``is_dry_run`` (the write is rolled back afterwards, so hooks can
    skip side effects such as e-mails). With this middleware installed, admin code can use
    these flags directly (no ``getattr``). Enforced by system check ``admin_api.E001``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_api = False
        request.is_mcp = False
        request.is_dry_run = False
        return self.get_response(request)
