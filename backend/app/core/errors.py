"""Domain errors and RFC 9457 problem-details responses."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 400
    title = "Bad request"

    def __init__(self, detail: str, errors: list[dict[str, Any]] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


class NotFound(AppError):
    status_code = 404
    title = "Not found"


class Conflict(AppError):
    status_code = 409
    title = "Conflict"


class Unauthorized(AppError):
    status_code = 401
    title = "Unauthorized"


class Forbidden(AppError):
    status_code = 403
    title = "Forbidden"


class TooManyRequests(AppError):
    status_code = 429
    title = "Too many requests"


class UnprocessableData(AppError):
    status_code = 422
    title = "Unprocessable data"


class ServiceUnavailable(AppError):
    status_code = 503
    title = "Service unavailable"


def problem(status: int, title: str, detail: str, errors: list[Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    if errors:
        body["errors"] = errors
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return problem(exc.status_code, exc.title, exc.detail, exc.errors)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return problem(422, "Validation error", "Request body or parameters are invalid", errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail), str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return problem(500, "Internal server error", "An unexpected error occurred")
