from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from app.core.config import settings

# Identifiant de corrélation propagé sur toute la durée d'une requête HTTP.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_RESERVED_LOGRECORD_KEYS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"asctime", "message", "taskName"}

# Champs à ne jamais écrire en clair dans les logs (DoD : aucune donnée sensible loggée).
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "mot_de_passe",
        "mot_de_passe_hash",
        "access_token",
        "refresh_token",
        "token",
        "authorization",
        "signature",
        "mrz_image",
        "raw_mrz_lines",
    }
)


def scrub(value: Any) -> Any:
    """Masque récursivement les valeurs des clés sensibles avant log."""
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_KEYS else scrub(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """Formatteur de logs structurés JSON avec identifiant de corrélation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or request_id_ctx.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOGRECORD_KEYS and key != "request_id":
                payload[key] = scrub(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_ctx.get()
        return True


def new_request_id() -> str:
    return uuid.uuid4().hex


def configure_logging() -> None:
    """Configure le logging racine ; appelé une seule fois au démarrage."""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if settings.LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.LOG_LEVEL.upper())

    # Uvicorn installe ses propres handlers : on les rebranche sur le nôtre.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
