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


class LockedAccountError(UnauthorizedError):
    """Trop de tentatives d'authentification échouées.

    Volontairement un 401 et non un 423 : la réponse est renvoyée à un appelant
    **non authentifié**, et le code d'erreur suffit au client pour afficher le
    bon message. Le compte n'est pas révélé comme existant pour autant — voir
    `AuthService.authenticate`.
    """

    error_code = "LOCKED_ACCOUNT"
    message = "Ce compte est temporairement verrouillé après plusieurs échecs de connexion."


class ForbiddenError(AppError):
    status_code = 403
    error_code = "FORBIDDEN"
    message = "Accès interdit."


class InactiveUserError(ForbiddenError):
    error_code = "INACTIVE_USER"
    message = "Ce compte est désactivé."


class InsufficientRoleError(ForbiddenError):
    error_code = "FORBIDDEN"
    message = "Cette opération est réservée aux administrateurs."


class NotFoundError(AppError):
    status_code = 404
    error_code = "NOT_FOUND"
    message = "Ressource introuvable."


class VisitNotFoundError(NotFoundError):
    error_code = "VISIT_NOT_FOUND"
    message = "Visite introuvable."


class VisitorNotFoundError(NotFoundError):
    error_code = "VISITOR_NOT_FOUND"
    message = "Visiteur introuvable."


class ServiceNotFoundError(NotFoundError):
    error_code = "SERVICE_NOT_FOUND"
    message = "Service introuvable."


class AgentNotFoundError(NotFoundError):
    error_code = "AGENT_NOT_FOUND"
    message = "Agent introuvable."


class PurposeNotFoundError(NotFoundError):
    error_code = "PURPOSE_NOT_FOUND"
    message = "Motif de visite introuvable."


class UserNotFoundError(NotFoundError):
    error_code = "USER_NOT_FOUND"
    message = "Compte utilisateur introuvable."


class SessionNotFoundError(NotFoundError):
    error_code = "SESSION_NOT_FOUND"
    message = "Session introuvable."


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


class VisitorAlreadyPresentError(ConflictError):
    """Le visiteur a déjà une visite ouverte : il ne peut pas entrer deux fois.

    Les `details` portent la visite en cours et son heure d'entrée, pour que le
    client propose la clôture plutôt que de renvoyer l'agent à un message d'erreur.
    """

    error_code = "VISITOR_ALREADY_PRESENT"
    message = (
        "Ce visiteur a déjà une visite en cours : clôturez-la avant d'en "
        "enregistrer une nouvelle."
    )


class VisitCancelledError(ConflictError):
    error_code = "VISIT_CANCELLED"
    message = "Cette visite est annulée : elle ne peut plus être modifiée."


class DuplicateIdentifiantError(ConflictError):
    error_code = "DUPLICATE_IDENTIFIANT"
    message = "Cet identifiant est déjà attribué à un autre compte."


class DuplicateReferentielError(ConflictError):
    error_code = "DUPLICATE_REFERENTIEL"
    message = "Une entrée portant cette valeur existe déjà."


class ArchivedReferentielError(ConflictError):
    error_code = "ARCHIVED_REFERENTIEL"
    message = "Cette entrée est archivée : elle ne peut plus être utilisée."


class SelfModificationError(ConflictError):
    """Garde-fou anti-verrouillage : un admin ne se retire pas ses propres accès.

    Sans lui, le dernier administrateur peut se désactiver ou se rétrograder et
    rendre le dashboard définitivement inaccessible — il faudrait alors passer par
    un accès direct à la base pour s'en sortir.
    """

    error_code = "SELF_MODIFICATION_FORBIDDEN"
    message = "Vous ne pouvez pas appliquer cette opération à votre propre compte."


class LastAdminError(ConflictError):
    error_code = "LAST_ADMIN"
    message = "Ce compte est le dernier administrateur actif : il doit le rester."


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


class NotImplementedYetError(AppError):
    status_code = 501
    error_code = "NOT_IMPLEMENTED"
    message = "Cette fonctionnalité n'est pas encore disponible."


class ExportFormatUnavailableError(NotImplementedYetError):
    error_code = "EXPORT_FORMAT_UNAVAILABLE"
    message = "Ce format d'export n'est pas encore disponible."


class InternalError(AppError):
    status_code = 500
    error_code = "INTERNAL_ERROR"
    message = "Une erreur interne est survenue."
