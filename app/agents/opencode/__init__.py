from app.agents.opencode.client import OpenCodeClient
from app.agents.opencode.discovery import discover_models
from app.agents.opencode.provider import provider
from app.agents.registry import registry

# Register provider as pending — it will self-register models after initialization
registry.register_pending(provider)

__all__ = ["OpenCodeClient", "discover_models", "provider"]
