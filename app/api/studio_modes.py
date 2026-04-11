"""
Studio mode policies — product-facing intent → SelectionPolicy mapping.

Each studio mode expresses WHAT the user wants (speed, balance, quality),
not WHICH specific model to use. The intelligence layer selects the best
available candidate at runtime based on availability, latency, stability,
quality scores, and capability tags.
This replaces the old fixed model→model mapping that made modes fragile
when a single provider went down.
"""



# ── Mode policies ──
#
# Each policy uses SelectionPolicy-compatible fields.
# When preferred_models is empty, the ModelSelector collects ALL available
# models from the unified registry and ranks them by intelligence.

STUDIO_MODE_POLICIES: dict[str, dict] = {
    # Fast: prioritize low latency, accept any viable model
    "fast": {
        "label": "Быстрый",
        "description": "Мгновенный ответ от самой быстрой доступной модели",
        "is_pipeline": False,
        # No preferred_models → let intelligence pick the fastest available
        "selection_policy": {
            "preferred_models": [],
            "preferred_tags": ["fast"],
            "avoid_tags": ["experimental"],
            "min_availability": 0.4,
            "max_latency_s": 30.0,
            "fallback_mode": "next_best",
            "max_fallback_attempts": 3,
        },
    },
    # Balanced: prefer stable, reasoning-strong models, good latency
    "balanced": {
        "label": "Сбалансированный",
        "description": "Модель с хорошим балансом скорости и качества",
        "is_pipeline": False,
        "selection_policy": {
            "preferred_models": [],
            "preferred_tags": ["stable", "reasoning_strong"],
            "avoid_tags": ["experimental"],
            "min_availability": 0.5,
            "max_latency_s": 60.0,
            "fallback_mode": "next_best",
            "max_fallback_attempts": 3,
        },
    },
    # Quality: pipeline with dynamic stage selection
    "quality": {
        "label": "Качество",
        "description": "Создать → Проверить → Улучшить",
        "is_pipeline": True,
        "pipeline_id": "generate-review-refine",
        # Stages use their own selection_policy (see builtin_pipelines update)
    },
    # Review: pipeline with dynamic stage selection
    "review": {
        "label": "Рецензия",
        "description": "Создать → Критика → Перегенерация",
        "is_pipeline": True,
        "pipeline_id": "generate-critique-regenerate",
    },
    # Deep: pipeline with dynamic stage selection
    "deep": {
        "label": "Глубокий",
        "description": "Черновик → Верификация → Финализация",
        "is_pipeline": True,
        "pipeline_id": "draft-verify-finalize",
    },
}


def get_mode_policy(mode: str) -> dict:
    """Get the selection policy for a studio mode."""
    return STUDIO_MODE_POLICIES.get(mode, STUDIO_MODE_POLICIES["balanced"])


def get_selection_policy(mode: str) -> dict | None:
    """Get the SelectionPolicy dict for a single-model mode.

    Returns None for pipeline modes (they handle selection per-stage).
    """
    policy = get_mode_policy(mode)
    if policy.get("is_pipeline"):
        return None
    return policy.get("selection_policy")
