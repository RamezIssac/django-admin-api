from __future__ import annotations

from django.http import JsonResponse


class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, detail: str | None = None, *, status: int | None = None, code: str | None = None, **extra):
        super().__init__(detail or self.code)
        self.detail = detail
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.extra = extra

    def as_response(self) -> JsonResponse:
        payload = {"error": self.code}
        if self.detail:
            payload["detail"] = self.detail
        payload.update(self.extra)
        return JsonResponse(payload, status=self.status)


class NotAuthenticated(ApiError):
    status = 401
    code = "not_authenticated"


class PermissionDeniedError(ApiError):
    status = 403
    code = "permission_denied"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class MethodNotAllowed(ApiError):
    status = 405
    code = "method_not_allowed"
