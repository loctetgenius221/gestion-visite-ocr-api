from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Erreur métier de base.

    Toutes les erreurs applicatives héritent de cette classe : elles sont converties
    par le handler global en réponse au format 5.6 de la spec
    (`{error_code, message, details}`), ce qui garantit un contrat d'erreur homogène
    et évite toute fuite de stacktrace vers le client.
    """

    status_code: int = 400
    error_code: str = "BAD_REQUEST"
    message: str = "Requête invalide."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any = None,
        error_code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details
        if error_code is not None:
            self.error_code = error_code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    error_code = "BAD_REQUEST"
    message = "Requête malformée."


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "UNAUTHORIZED"
    message = "Authentification requise ou invalide."


class InvalidCredentialsError(UnauthorizedError):
    error_code = "INVALID_CREDENTIALS"
    message = "Identifiant ou mot de passe incorrect."


class InvalidTokenError(UnauthorizedError):
    error_code = "INVALID_TOKEN"
    message = "Le token fourni est invalide ou a expiré."


class ForbiddenError(AppError):
    status_code = 403
    error_code = "FORBIDDEN"
    message = "Accès interdit."


class InactiveUserError(ForbiddenError):
    error_code = "INACTIVE_USER"
    message = "Ce compte est désactivé."


class NotFoundError(AppError):
    status_code = 404
    error_code = "NOT_FOUND"
    message = "Ressource introuvable."


class VisitNotFoundError(NotFoundError):
    error_code = "VISIT_NOT_FOUND"
    message = "Visite introuvable."


class ServiceNotFoundError(NotFoundError):
    error_code = "SERVICE_NOT_FOUND"
    message = "Service introuvable."


class AgentNotFoundError(NotFoundError):
    error_code = "AGENT_NOT_FOUND"
    message = "Agent introuvable."


class PurposeNotFoundError(NotFoundError):
    error_code = "PURPOSE_NOT_FOUND"
    message = "Motif de visite introuvable."


class ConflictError(AppError):
    status_code = 409
    error_code = "CONFLICT"
    message = "Conflit avec l'état actuel de la ressource."


class VisitAlreadyClosedError(ConflictError):
    error_code = "VISIT_ALREADY_CLOSED"
    message = "Cette visite a déjà été clôturée."


class DuplicateVisitError(ConflictError):
    error_code = "DUPLICATE_VISIT"
    message = "Cette visite a déjà été enregistrée."


class UnprocessableError(AppError):
    status_code = 422
    error_code = "UNPROCESSABLE_ENTITY"
    message = "La requête n'a pas pu être traitée."


class MrzNotDetectedError(UnprocessableError):
    error_code = "MRZ_NOT_DETECTED"
    message = "Aucune zone MRZ n'a pu être détectée sur l'image fournie."


class MrzParsingError(UnprocessableError):
    error_code = "MRZ_PARSING_FAILED"
    message = "La zone MRZ détectée n'a pas pu être interprétée."


class UnsupportedImageError(BadRequestError):
    error_code = "UNSUPPORTED_IMAGE"
    message = "Format d'image non supporté. Formats acceptés : jpg, jpeg, png, heic."


class FileTooLargeError(BadRequestError):
    error_code = "FILE_TOO_LARGE"
    message = "Le fichier envoyé dépasse la taille maximale autorisée."


class InternalError(AppError):
    status_code = 500
    error_code = "INTERNAL_ERROR"
    message = "Une erreur interne est survenue."
