"""Custom exception classes with HTTP status code mappings."""
from typing import Any


class AppException(Exception):
    """Base application exception."""

    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        details: list[Any] | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.details = details or []
        super().__init__(self.message)


class NotFoundError(AppException):
    status_code = 404
    error_code = "NOT_FOUND"
    message = "Resource not found"


class UnauthorizedError(AppException):
    status_code = 401
    error_code = "UNAUTHORIZED"
    message = "Authentication required"


class ForbiddenError(AppException):
    status_code = 403
    error_code = "FORBIDDEN"
    message = "You do not have permission to perform this action"


class ValidationError(AppException):
    status_code = 422
    error_code = "VALIDATION_ERROR"
    message = "Validation failed"


class ConflictError(AppException):
    status_code = 409
    error_code = "CONFLICT"
    message = "Resource already exists"


class PaymentError(AppException):
    status_code = 402
    error_code = "PAYMENT_FAILED"
    message = "Payment processing failed"


class ServiceUnavailableError(AppException):
    status_code = 503
    error_code = "SERVICE_UNAVAILABLE"
    message = "External service temporarily unavailable"


class MOQViolationError(ValidationError):
    error_code = "MOQ_VIOLATION"
    message = "Minimum order quantity not met"


class MOVViolationError(ValidationError):
    error_code = "MOV_VIOLATION"
    message = "Minimum order value not met"


class InsufficientStockError(ValidationError):
    error_code = "INSUFFICIENT_STOCK"
    message = "Insufficient stock for one or more items"


class AccountSuspendedError(ForbiddenError):
    error_code = "ACCOUNT_SUSPENDED"
    message = "Your account has been suspended"


class AccountNotActivatedError(ForbiddenError):
    error_code = "ACCOUNT_NOT_ACTIVATED"
    message = "Your account is not yet activated. Check your email for the activation link."
