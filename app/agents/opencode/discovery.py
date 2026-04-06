from app.agents.opencode.client import OpenCodeClient
from app.agents.registry import AgentModelDefinition
from app.core.logging import get_logger

logger = get_logger(__name__)


async def discover_models(client: OpenCodeClient) -> list[AgentModelDefinition]:
    """
    Discover FREE models from OpenCode's built-in provider.

    Strategy:
    - The `opencode` provider (id="opencode") ships with curated free models
      that don't require user API keys (OPENCODE_API_KEY is internal)
    - We extract models directly from this provider in /provider registry
    - Other providers (openai, lmstudio, openrouter, etc.) require separate auth — skipped

    The set of free models may change over time — we always discover from live instance.
    """
    try:
        registry_data = await client.get_provider_registry()
    except Exception as exc:
        logger.warning(
            "OpenCode model discovery failed: /provider endpoint",
            error=str(exc),
        )
        return []

    # Build provider info map
    all_providers = registry_data.get("all", [])
    provider_map: dict[str, dict] = {}
    for p in all_providers:
        if isinstance(p, dict) and "id" in p:
            provider_map[p["id"]] = p

    # The opencode built-in provider has free curated models
    opencode_provider = provider_map.get("opencode", {})
    if not opencode_provider:
        logger.info("OpenCode built-in provider not found — no free models available")
        return []

    models_dict = opencode_provider.get("models", {})
    provider_display = opencode_provider.get("name", "OpenCode")

    models: list[AgentModelDefinition] = []
    for mid in models_dict:
        canonical_id = f"agent/opencode/opencode/{mid}"
        models.append(
            AgentModelDefinition(
                id=canonical_id,
                provider_id="opencode",
                transport="agent",
                source_type="opencode_server",
                enabled=True,
                available=True,
                discovered_from_provider="opencode",
                requires_auth=False,
                provider_connected=False,
                source_kind="zen",
                is_runtime_available=True,
                metadata={
                    "display_name": f"{provider_display} - {mid}",
                    "provider_key": "opencode",
                    "model_id": mid,
                    "agent_type": "opencode_server",
                    "source_kind": "zen",
                    "requires_auth": False,
                    "provider_connected": False,
                    "provider_display_name": provider_display,
                },
            )
        )

    logger.info(
        "OpenCode free model discovery completed",
        models_found=len(models),
        models=[m.id for m in models],
    )

    return models
