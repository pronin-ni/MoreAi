"""
Capability tags registry and built-in tag definitions.

Tags describe model/provider characteristics used in selection:
- fast, stable, creative, review_strong, reasoning_strong, etc.

Tags are assigned to models/providers and used in ranking/scoring.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.intelligence.types import CapabilityEntry, CapabilityTag

logger = get_logger(__name__)


# ── Built-in capability tag assignments ──
# These are practical proxy assignments based on known model characteristics.
# They can be overridden via admin config.

BUILTIN_TAG_ASSIGNMENTS: list[CapabilityEntry] = [
    # Qwen — strong reasoning, stable, good for generation and refinement
    CapabilityEntry(
        tag=CapabilityTag.REASONING_STRONG,
        description="Strong reasoning capabilities",
        applies_to_models=["qwen", "browser/qwen"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.STABLE,
        description="Consistently available and reliable",
        applies_to_models=["qwen", "browser/qwen"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.LONG_CONTEXT,
        description="Supports long context windows",
        applies_to_models=["qwen", "browser/qwen"],
    ),

    # GLM — good for review and critique tasks
    CapabilityEntry(
        tag=CapabilityTag.REVIEW_STRONG,
        description="Strong at reviewing and critiquing text",
        applies_to_models=["glm", "browser/glm"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.REASONING_STRONG,
        description="Strong reasoning capabilities",
        applies_to_models=["glm", "browser/glm"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.STABLE,
        description="Consistently available and reliable",
        applies_to_models=["glm", "browser/glm"],
    ),

    # Kimi — creative, good for generation
    CapabilityEntry(
        tag=CapabilityTag.CREATIVE,
        description="Good creative writing and generation",
        applies_to_models=["kimi", "browser/kimi"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.FAST,
        description="Fast response times",
        applies_to_models=["kimi", "browser/kimi"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.BROWSER_ONLY,
        description="Only available via browser automation",
        applies_to_providers=["browser/kimi"],
    ),

    # ChatGPT — strong reasoning, creative
    CapabilityEntry(
        tag=CapabilityTag.CREATIVE,
        description="Good creative writing and generation",
        applies_to_models=["chatgpt", "browser/chatgpt"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.REASONING_STRONG,
        description="Strong reasoning capabilities",
        applies_to_models=["chatgpt", "browser/chatgpt"],
    ),

    # Yandex — fast, stable
    CapabilityEntry(
        tag=CapabilityTag.FAST,
        description="Fast response times",
        applies_to_models=["yandex", "browser/yandex"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.STABLE,
        description="Consistently available and reliable",
        applies_to_models=["yandex", "browser/yandex"],
    ),

    # Deepseek — code strong, reasoning
    CapabilityEntry(
        tag=CapabilityTag.CODE_STRONG,
        description="Strong code generation capabilities",
        applies_to_models=["deepseek", "browser/deepseek"],
    ),
    CapabilityEntry(
        tag=CapabilityTag.REASONING_STRONG,
        description="Strong reasoning capabilities",
        applies_to_models=["deepseek", "browser/deepseek"],
    ),
]


class CapabilityRegistry:
    """Registry of capability tags for models and providers.

    Supports built-in and dynamically assigned tags.
    """

    def __init__(self) -> None:
        self._tags: dict[str, list[CapabilityEntry]] = {}
        self._model_tags: dict[str, set[str]] = {}
        self._provider_tags: dict[str, set[str]] = {}
        self._initialized = False

    def initialize(self) -> None:
        """Load built-in tag assignments."""
        if self._initialized:
            return

        for entry in BUILTIN_TAG_ASSIGNMENTS:
            tag_str = entry.tag.value
            self._tags.setdefault(tag_str, []).append(entry)

            for model_id in entry.applies_to_models:
                self._model_tags.setdefault(model_id, set()).add(tag_str)

            for provider_id in entry.applies_to_providers:
                self._provider_tags.setdefault(provider_id, set()).add(tag_str)

        self._initialized = True
        logger.info(
            "capability_registry_initialized",
            tag_count=str(len(self._tags)),
            model_count=str(len(self._model_tags)),
            provider_count=str(len(self._provider_tags)),
        )

    def get_tags_for_model(self, model_id: str) -> set[str]:
        """Get capability tags for a model."""
        return self._model_tags.get(model_id, set()).copy()

    def get_tags_for_provider(self, provider_id: str) -> set[str]:
        """Get capability tags for a provider."""
        return self._provider_tags.get(provider_id, set()).copy()

    def get_tags(self, model_id: str, provider_id: str) -> set[str]:
        """Get combined capability tags for a model+provider."""
        model_tags = self._model_tags.get(model_id, set())
        provider_tags = self._provider_tags.get(provider_id, set())
        return model_tags | provider_tags

    def has_tag(self, model_id: str, provider_id: str, tag: str) -> bool:
        """Check if a model+provider has a specific tag."""
        return tag in self.get_tags(model_id, provider_id)

    def add_tag(self, model_id: str, provider_id: str, tag: str) -> None:
        """Dynamically add a tag to a model+provider."""
        self._model_tags.setdefault(model_id, set()).add(tag)
        self._provider_tags.setdefault(provider_id, set()).add(tag)
        self._tags.setdefault(tag, []).append(
            CapabilityEntry(
                tag=CapabilityTag(tag),  # type: ignore[arg-type]
                description=f"Dynamically assigned tag: {tag}",
                applies_to_models=[model_id],
                applies_to_providers=[provider_id],
            ),
        )

    def remove_tag(self, model_id: str, provider_id: str, tag: str) -> None:
        """Remove a tag from a model+provider."""
        self._model_tags.get(model_id, set()).discard(tag)
        self._provider_tags.get(provider_id, set()).discard(tag)

    def list_all_tags(self) -> dict[str, list[dict]]:
        """List all registered tags with their assignments."""
        result: dict[str, dict] = {}
        for tag_str, entries in self._tags.items():
            result[tag_str] = {
                "description": entries[0].description if entries else "",
                "models": list({m for e in entries for m in e.applies_to_models}),
                "providers": list({p for e in entries for p in e.applies_to_providers}),
            }
        return result

    def get_models_by_tag(self, tag: str) -> list[str]:
        """Get all models that have a specific tag."""
        return [
            model_id
            for model_id, tags in self._model_tags.items()
            if tag in tags
        ]

    def clear(self) -> None:
        """Clear all tags (for testing)."""
        self._tags.clear()
        self._model_tags.clear()
        self._provider_tags.clear()
        self._initialized = False


# Global singleton
capability_registry = CapabilityRegistry()
