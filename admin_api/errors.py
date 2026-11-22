from __future__ import annotations

from django.http import JsonResponse
from django.utils.translation import gettext_lazy


class ApiError(Exception):
    status = 400
    code = "bad_request"
    default_detail: str | None = None  # a gettext_lazy string, translated when raised

    def __init__(self, detail: str | None = None, *, status: int | None = None, code: str | None = None, **extra):
        if detail is None and self.default_detail:
            detail = str(self.default_detail)
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


class InvalidArguments(ApiError):
    status = 400
    code = "invalid_arguments"


class NotAuthenticated(ApiError):
    status = 401
    code = "not_authenticated"


class PermissionDeniedError(ApiError):
    status = 403
    code = "permission_denied"
    default_detail = gettext_lazy("You do not have permission to do this.")


class NotFound(ApiError):
    status = 404
    code = "not_found"
    default_detail = gettext_lazy("This kind of record is not available here.")


class MethodNotAllowed(ApiError):
    status = 405
    code = "method_not_allowed"


class PayloadTooLarge(ApiError):
    status = 413
    code = "payload_too_large"
    default_detail = gettext_lazy("The request body is larger than the server's DATA_UPLOAD_MAX_MEMORY_SIZE.")
