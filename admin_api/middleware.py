class ApiRequestMiddleware:
    """Sets ``request.is_api = False`` on every request.

    API views flip it to ``True``. With this middleware installed, admin code can use
    ``request.is_api`` directly (no ``getattr``). Enforced by system check ``admin_api.E001``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_api = False
        return self.get_response(request)
