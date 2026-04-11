"""
Tracing foundation — lightweight, OpenTelemetry-compatible structure.

Not a full tracing implementation yet. Provides:
- Span abstraction with parent-child relationships
- Context propagation via ContextVar
- Structured span events
- Easy upgrade path to OpenTelemetry later

Usage:
    async with trace_span("process_completion", model="browser/qwen") as span:
        span.set_attribute("provider_id", "qwen")
        span.add_event("resolved_model", transport="browser")
        ...
"""

import time
import uuid
import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Current span context — accessible for middleware/integration
current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("current_span", default=None)


@dataclass(slots=True)
class SpanEvent:
    """Point-in-time event within a span."""
    name: str
    timestamp: float
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Span:
    """Single trace span."""
    trace_id: str
    span_id: str
    name: str
    parent_id: str | None
    start_time: float
    end_time: float | None = None
    status: str = "ok"  # ok, error
    status_message: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    events: list[SpanEvent] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    def set_attribute(self, key: str, value: str) -> None:
        self.attributes[key] = str(value)

    def set_status(self, status: str, message: str = "") -> None:
        self.status = status
        self.status_message = message

    def add_event(self, name: str, **attributes: str) -> None:
        self.events.append(SpanEvent(
            name=name,
            timestamp=time.monotonic(),
            attributes=attributes,
        ))

    def to_log_dict(self) -> dict[str, Any]:
        """Export span as structured log dict."""
        result = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "span_name": self.name,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            **self.attributes,
        }
        if self.parent_id:
            result["parent_span_id"] = self.parent_id
        if self.status_message:
            result["status_message"] = self.status_message
        if self.events:
            result["span_events"] = [
                {"name": e.name, "attributes": e.attributes}
                for e in self.events
            ]
        return result

    def __repr__(self) -> str:
        dur = f"{self.duration_seconds:.3f}s" if self.duration_seconds is not None else "active"
        return f"Span({self.name}, {dur}, status={self.status})"


@asynccontextmanager
async def trace_span(name: str, **initial_attributes: str):
    """Create a span with parent from current context.

    Usage:
        async with trace_span("process_completion", model="browser/qwen") as span:
            span.set_attribute("provider_id", "qwen")
            span.add_event("resolved")
    """
    parent = current_span.get()
    span = Span(
        trace_id=parent.trace_id if parent else uuid.uuid4().hex[:16],
        span_id=uuid.uuid4().hex[:8],
        name=name,
        parent_id=parent.span_id if parent else None,
        start_time=time.monotonic(),
        attributes=dict(initial_attributes),
    )

    token = current_span.set(span)
    try:
        yield span
    except Exception as e:
        span.set_status("error", str(e))
        span.add_event("error", error_type=type(e).__name__)
        logger.warning(
            "Span error",
            **span.to_log_dict(),
        )
        raise
    finally:
        span.end_time = time.monotonic()
        current_span.reset(token)

        # Log completed spans at debug level (only for top-level or errors)
        if parent is None or span.status == "error":
            logger.info(
                "Span completed",
                **span.to_log_dict(),
            )
