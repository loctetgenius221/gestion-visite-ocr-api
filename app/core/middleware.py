from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger, new_request_id, request_id_ctx

logger = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attache un identifiant de corrélation à chaque requête et trace son issue.

    L'identifiant est repris de l'en-tête `X-Request-ID` s'il est fourni par le
    client (utile pour corréler avec les logs de l'app mobile), sinon généré.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # Le handler global produira la réponse 500 ; on trace ici la durée.
            logger.exception(
                "Requête en erreur",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            raise
        finally:
            request_id_ctx.reset(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "Requête traitée",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "request_id": request_id,
            },
        )
        return response
