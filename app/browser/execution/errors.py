from dataclasses import dataclass, field
from typing import Any

from app.core.errors import GatewayTimeoutError, InternalError, ServiceUnavailableError


@dataclass(slots=True)
class BrowserTaskError(Exception):
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    failure_kind: str = "execution"

    def to_api_error(self):
        payload = {
            **self.details,
            "retryable": self.retryable,
            "failure_kind": self.failure_kind,
        }
        return InternalError(self.message, details=payload)


@dataclass(slots=True)
class RetryableBrowserTaskError(BrowserTaskError):
    retryable: bool = True
    failure_kind: str = "transient_browser_failure"

    def to_api_error(self):
        payload = {
            **self.details,
            "retryable": self.retryable,
            "failure_kind": self.failure_kind,
        }
        return ServiceUnavailableError(self.message, details=payload)


@dataclass(slots=True)
class QueueWaitTimeoutError(BrowserTaskError):
    failure_kind: str = "queue_timeout"

    def to_api_error(self):
        payload = {
            **self.details,
            "retryable": self.retryable,
            "failure_kind": self.failure_kind,
        }
        return ServiceUnavailableError(self.message, details=payload)


@dataclass(slots=True)
class ProviderCircuitOpenError(BrowserTaskError):
    failure_kind: str = "provider_circuit_open"

    def to_api_error(self):
        payload = {
            **self.details,
            "retryable": self.retryable,
            "failure_kind": self.failure_kind,
        }
        return ServiceUnavailableError(self.message, details=payload)


@dataclass(slots=True)
class ExecutionTimeoutError(BrowserTaskError):
    retryable: bool = True
    failure_kind: str = "execution_timeout"

    def to_api_error(self):
        payload = {
            **self.details,
            "retryable": self.retryable,
            "failure_kind": self.failure_kind,
        }
        return GatewayTimeoutError(self.message, details=payload)
