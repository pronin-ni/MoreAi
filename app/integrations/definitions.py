from pathlib import Path

from app.integrations.parser_ready_to_use import parse_ready_to_use_markdown
from app.integrations.types import IntegrationDefinition

SNAPSHOT_PATH = Path(__file__).with_name("ready_to_use_snapshot.md")


def load_ready_to_use_markdown() -> str:
    return SNAPSHOT_PATH.read_text(encoding="utf-8")


def build_integration_definitions() -> list[IntegrationDefinition]:
    parsed = parse_ready_to_use_markdown(load_ready_to_use_markdown())
    definitions: list[IntegrationDefinition] = []

    base_url_ids = {
        "https://localhost:1337/v1": "g4f-localhost",
        "https://g4f.space/api/groq": "g4f-groq",
        "https://g4f.space/api/ollama": "g4f-ollama",
        "https://g4f.space/api/pollinations": "g4f-pollinations",
        "https://g4f.space/api/nvidia": "g4f-nvidia",
        "https://g4f.space/api/gemini": "g4f-gemini",
        "https://g4f.space/v1": "g4f-hosted",
    }
    for entry in parsed.base_urls:
        integration_id = base_url_ids[entry["base_url"]]
        requires_key = entry["api_key"] == "required"
        definitions.append(
            IntegrationDefinition(
                integration_id=integration_id,
                display_name=integration_id.replace("g4f-", "G4F ").replace("-", " ").title(),
                integration_type="openai_compatible",
                group="ready_to_use_base_url",
                source_type="g4f_openai",
                base_url=entry["base_url"],
                api_key_requirement="required" if requires_key else "none",
                notes=entry["notes"],
                enabled_by_default=not requires_key,
                fallback_models=["default"] if integration_id == "g4f-localhost" else [],
            )
        )

    route_ids = {
        "Nvidia": "nvidia-api",
        "DeepInfra": "deepinfra",
        "OpenRouter": "openrouter",
        "Google Gemini": "gemini-openai",
        "xAI": "xai",
        "Together": "together",
        "OpenAI": "openai",
        "TypeGPT": "typegpt",
        "Grok": "grok",
        "ApiAirforce": "apiairforce",
        "Auto Provider & Model Selection": "g4f-auto",
    }
    for entry in parsed.supported_api_routes:
        integration_id = route_ids[entry["display_name"]]
        definitions.append(
            IntegrationDefinition(
                integration_id=integration_id,
                display_name=entry["display_name"],
                integration_type="openai_compatible",
                group="supported_api_route",
                source_type="g4f_openai" if integration_id == "g4f-auto" else "external_api",
                base_url=entry["base_url"],
                api_key_requirement="unknown",
                enabled_by_default=integration_id == "g4f-auto",
                fallback_models=["default"] if integration_id == "g4f-auto" else [],
            )
        )

    client_defaults = {
        "Pollinations AI": "https://g4f.space/api/pollinations",
        "Ollama": "https://g4f.space/api/ollama",
        "Gemini": "https://g4f.space/api/gemini",
    }
    client_ids = {
        "Pollinations AI": "g4f-client-pollinations",
        "Puter AI": "g4f-client-puter",
        "HuggingFace": "g4f-client-huggingface",
        "Ollama": "g4f-client-ollama",
        "Gemini": "g4f-client-gemini",
        "OpenAI Chat": "g4f-client-openai-chat",
        "Perplexity": "g4f-client-perplexity",
    }
    for entry in parsed.individual_clients:
        display_name = entry["display_name"]
        definitions.append(
            IntegrationDefinition(
                integration_id=client_ids[display_name],
                display_name=display_name,
                integration_type="client_based",
                group="individual_client",
                source_type="g4f_client",
                base_url=client_defaults.get(display_name),
                api_key_requirement="unknown",
                notes=entry["docs_url"],
                enabled_by_default=display_name in client_defaults,
                fallback_models=["default"] if display_name not in client_defaults else [],
            )
        )

    definitions.append(
        IntegrationDefinition(
            integration_id="ollamafreeapi",
            display_name="OllamaFreeAPI",
            integration_type="client_based",
            group="individual_client",
            source_type="client_based",
            base_url=None,
            api_key_requirement="none",
            notes="Python client-based free distributed Ollama API",
            enabled_by_default=True,
            default_timeout_seconds=60,
        )
    )

    return definitions


READY_TO_USE_DEFINITIONS = build_integration_definitions()
