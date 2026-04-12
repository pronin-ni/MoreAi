"""
Kilocode agent provider integration.

Provides managed/external lifecycle for Kilocode server mode,
model discovery, and prompt completion via Kilocode HTTP API.
"""

from app.agents.kilocode.client import KilocodeClient
from app.agents.kilocode.discovery import discover_models
from app.agents.kilocode.provider import provider
from app.agents.registry import registry

# Register provider as pending — it will self-register models after initialization
registry.register_pending(provider)

__all__ = ["KilocodeClient", "discover_models", "provider"]
