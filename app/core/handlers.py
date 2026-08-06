"""Handlers d'exceptions : toute erreur sortante suit le format de la spec §5.6."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Traduction des codes HTTP levés par Starlette/FastAPI vers nos `error_code` métier.
_HTTP_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "UNPROCESSABLE_ENTITY",
    429: "TOO_MANY_REQUESTS",
    503: "SERVICE_UNAVAILABLE",
}


def _error_response(
    status_code: int, error_code: str, message: str, details: object = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_error({"error_code": error_code, "message": message, "details": details}),
    )


def jsonable_error(payload: dict) -> dict:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(payload)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "Erreur applicative",
            extra={
                "error_code": exc.error_code,
                "path": request.url.path,
                "status": exc.status_code,
            },
        )
        return _error_response(exc.status_code, exc.error_code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 422 est réservé aux échecs métier par la spec §5.6 ; une charge utile
        # malformée relève d'un 400.
        return _error_response(
            status.HTTP_400_BAD_REQUEST,
            "VALIDATION_ERROR",
            "Les données envoyées sont invalides.",
            exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        error_code = _HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
        return _error_response(exc.status_code, error_code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # La stacktrace part dans les logs, jamais dans la réponse (spec §5.6).
        logger.exception("Erreur non gérée", extra={"path": request.url.path})
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            "Une erreur interne est survenue.",
        )
