import structlog
import logging
import sys
from contextvars import ContextVar
from typing import Optional
from uuid import uuid4

request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def configure_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper()),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(name)


def bind_request_id(request_id: str | None = None) -> str:
    if request_id is None:
        request_id = str(uuid4())
    request_id_ctx.set(request_id)
    return request_id


def clear_request_id() -> None:
    request_id_ctx.set(None)
