from typing import Any, Optional
from fastapi import HTTPException, status


class ProxyError(Exception):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class BrowserError(ProxyError):
    pass


class ChatNotReadyError(BrowserError):
    pass


class MessageInputNotFoundError(BrowserError):
    pass


class SendButtonNotFoundError(BrowserError):
    pass


class NewChatButtonNotFoundError(BrowserError):
    pass


class AssistantMessageNotFoundError(BrowserError):
    pass


class GenerationTimeoutError(BrowserError):
    pass


class APIError(HTTPException):
    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str = "internal_error",
        details: Optional[dict[str, Any]] = None,
    ):
        self.error_type = error_type
        self.details = details or {}
        super().__init__(
            status_code=status_code,
            detail={
                "message": message,
                "type": error_type,
                "details": self.details,
            },
        )


class BadRequestError(APIError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            message=message,
            error_type="invalid_request_error",
            details=details,
        )


class NotFoundError(APIError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            message=message,
            error_type="not_found_error",
            details=details,
        )


class InternalError(APIError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=message,
            error_type="internal_error",
            details=details,
        )


class ServiceUnavailableError(APIError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            message=message,
            error_type="service_unavailable",
            details=details,
        )
