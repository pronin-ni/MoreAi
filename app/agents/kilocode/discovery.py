"""
Discover FREE models from Kilocode's built-in provider.

Strategy:
- The `kilocode` provider (id="kilocode") ships with curated free models
  that don't require user API keys
- We extract models directly from this provider in /provider registry
- Other providers (openai, anthropic, etc.) require separate auth — skipped

The set of free models may change over time — we always discover from live instance.
"""

from app.agents.kilocode.client import KilocodeClient
from app.agents.registry import AgentModelDefinition
from app.core.logging import get_logger

logger = get_logger(__name__)


async def discover_models(client: KilocodeClient) -> list[AgentModelDefinition]:
    """Discover free models from Kilocode's built-in provider."""
    try:
        registry_data = await client.get_provider_registry()
    except Exception as exc:
        logger.warning(
            "Kilocode model discovery failed: /provider endpoint",
            error=str(exc),
        )
        return []

    # Build provider info map
    all_providers = registry_data.get("all", [])
    provider_map: dict[str, dict] = {}
    for p in all_providers:
        if isinstance(p, dict) and "id" in p:
            provider_map[p["id"]] = p

    # The kilocode built-in provider has free curated models
    kilocode_provider = provider_map.get("kilocode", {})
    if not kilocode_provider:
        logger.info("Kilocode built-in provider not found — no free models available")
        return []

    models_dict = kilocode_provider.get("models", {})
    provider_display = kilocode_provider.get("name", "Kilocode")

    models: list[AgentModelDefinition] = []
    for mid in models_dict:
        canonical_id = f"agent/kilocode/kilocode/{mid}"
        models.append(
            AgentModelDefinition(
                id=canonical_id,
                provider_id="kilocode",
                transport="agent",
                source_type="kilocode_server",
                enabled=True,
                available=True,
                discovered_from_provider="kilocode",
                requires_auth=False,
                provider_connected=False,
                source_kind="zen",
                is_runtime_available=True,
                metadata={
                    "display_name": f"{provider_display} - {mid}",
                    "provider_key": "kilocode",
                    "model_id": mid,
                    "agent_type": "kilocode_server",
                    "source_kind": "zen",
                    "requires_auth": False,
                    "provider_connected": False,
                    "provider_display_name": provider_display,
                },
            )
        )

    logger.info(
        "Kilocode free model discovery completed",
        models_found=len(models),
        models=[m.id for m in models],
    )

    return models
