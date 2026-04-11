"""
Studio UI routes.

Provides a product-facing "smart chat" interface with pipeline awareness.
Separate from /ui — /ui remains the simple HTMX chat.

Endpoints:
- GET /studio — renders the studio page
- POST /studio/chat — sends a message, executes model or pipeline
- GET /studio/executions/{execution_id} — execution details for expandable view
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import bleach
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.api.routes_ui import render_markdown
from app.core.logging import get_logger
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.services.chat_proxy_service import service

logger = get_logger(__name__)

router = APIRouter()

# ── Template setup ──

_template_env = Environment(
    loader=FileSystemLoader("app/templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


def _render_template(name: str, **context: object) -> str:
    tmpl = _template_env.get_template(name)
    return tmpl.render(**context)


# ── Mode mapping ──

# Product-facing modes → backend strategy
# Each mode maps to either a single model or a pipeline_id.
# Users never see raw "pipeline/..." IDs.

STUDIO_MODES: dict[str, dict] = {
    "fast": {
        "label": "Fast",
        "description": "Quick response from the fastest available model",
        "type": "model",
        "model": "kimi",  # Fast model by convention
    },
    "balanced": {
        "label": "Balanced",
        "description": "Default model — good speed and quality",
        "type": "model",
        "model": "qwen",  # Default model
    },
    "quality": {
        "label": "Quality",
        "description": "Generate → Review → Refine pipeline for higher quality",
        "type": "pipeline",
        "pipeline_id": "generate-review-refine",
    },
    "review": {
        "label": "Review",
        "description": "Generate → Critique → Regenerate — deep analysis mode",
        "type": "pipeline",
        "pipeline_id": "generate-critique-regenerate",
    },
    "deep": {
        "label": "Deep",
        "description": "Draft → Verify → Finalize — thorough fact-checked response",
        "type": "pipeline",
        "pipeline_id": "draft-verify-finalize",
    },
}


def _resolve_mode(mode: str) -> dict:
    """Resolve a studio mode to its backend strategy.

    Returns a dict with type, model/pipeline_id, and metadata.
    Falls back to 'balanced' for unknown modes.
    """
    return STUDIO_MODES.get(mode, STUDIO_MODES["balanced"])


# ── Routes ──


@router.get("/studio")
async def studio_page():
    """Render the Studio UI page."""
    from app.pipeline.types import pipeline_registry
    from app.services.model_registry_service import service as model_registry_service

    # Get available modes
    modes = []
    for mode_key, mode_config in STUDIO_MODES.items():
        is_pipeline = mode_config["type"] == "pipeline"
        available = True
        if is_pipeline:
            p_def = pipeline_registry.get(mode_config["pipeline_id"])
            available = p_def is not None and p_def.enabled if p_def else False

        modes.append({
            "key": mode_key,
            "label": mode_config["label"],
            "description": mode_config["description"],
            "available": available,
            "is_pipeline": is_pipeline,
        })

    # Get grouped models for advanced mode
    browser_models, api_models, agent_models = model_registry_service.group_models()

    return HTMLResponse(content=_render_template(
        "studio.html",
        modes=modes,
        default_mode="balanced",
        browser_models=browser_models,
        api_models=api_models,
        agent_models=agent_models,
    ))


@router.post("/studio/chat")
async def studio_chat(
    request: Request,
    mode: str = Form(default="balanced"),
    message: str = Form(default=""),
    conversation_json: str = Form(default="[]"),
    action: str = Form(default=""),
    advanced_model: str = Form(default=""),
    advanced_type: str = Form(default="standard"),  # standard or custom_model
):
    """Process a studio chat message.

    Resolves the mode to either a single model or a pipeline,
    executes the request, and returns HTML partials with execution metadata.
    """
    # Handle clear action
    if action == "clear":
        return _render_studio_response(
            messages=[],
            conversation=[],
            mode=mode,
            status="cleared",
        )

    if not message:
        return _render_studio_response(
            messages=[],
            conversation=_safe_parse_json(conversation_json),
            mode=mode,
            status="error",
            error_message="Empty message",
        )

    # Parse conversation
    conversation = _safe_parse_json(conversation_json)

    # Resolve mode to backend strategy
    mode_config = _resolve_mode(mode)

    # Determine what to execute
    execution_type = mode_config["type"]  # "model" or "pipeline"
    target_model = ""
    pipeline_id = ""

    if advanced_type == "custom_model" and advanced_model:
        # Advanced: user selected a specific model
        target_model = advanced_model
        execution_type = "model"
    elif execution_type == "pipeline":
        pipeline_id = mode_config["pipeline_id"]
        target_model = f"pipeline/{pipeline_id}"
    else:
        target_model = mode_config["model"]

    # Append user message
    user_msg = {
        "role": "user",
        "content": message,
        "timestamp": time.time(),
    }
    conversation.append(user_msg)

    # Build request
    messages = [ChatMessage(**m) for m in conversation if m.get("role") in ("user", "assistant")]
    chat_request = ChatCompletionRequest(
        model=target_model,
        messages=messages,
        stream=False,
    )

    request_id = f"studio-{uuid.uuid4().hex[:8]}"
    execution_id = request_id  # Use request_id as execution_id for studio
    execution_summary: dict = {
        "execution_id": execution_id,
        "mode": mode,
        "execution_type": execution_type,
        "target_model": target_model,
        "pipeline_id": pipeline_id,
        "stage_count": 0,
        "selected_models": [],
        "fallback_count": 0,
        "quality_score": 0.0,
        "duration_ms": 0.0,
        "status": "success",
        "error": "",
    }

    started = time.monotonic()

    try:
        if execution_type == "pipeline":
            # Execute via pipeline executor
            from app.pipeline.executor import pipeline_executor
            from app.pipeline.types import pipeline_registry

            p_def = pipeline_registry.get(pipeline_id)
            if p_def is None:
                raise ValueError(f"Pipeline '{pipeline_id}' not found")

            response = await pipeline_executor.execute(p_def, chat_request, request_id)

            # Extract execution metadata
            execution_summary["stage_count"] = len(p_def.stages)
            execution_summary["selected_models"] = list({
                s.target_model for s in p_def.stages if s.target_model
            })
        else:
            # Execute via chat proxy service (single model)
            response = await service.process_completion(chat_request, request_id)
            execution_summary["stage_count"] = 1
            execution_summary["selected_models"] = [target_model]

        elapsed_ms = (time.monotonic() - started) * 1000
        execution_summary["duration_ms"] = round(elapsed_ms, 1)

        # Extract response content
        content = ""
        if response.choices:
            content = response.choices[0].message.content or ""

        # Append assistant response
        assistant_msg = {
            "role": "assistant",
            "content": content,
            "timestamp": time.time(),
        }
        conversation.append(assistant_msg)

        # Append assistant response
        render_messages = _build_render_messages(conversation)

        # Try to get quality score from execution context
        try:
            from app.pipeline.observability.scoring_history import scoring_history_store
            # Try to find recent quality data for this model
            for m in execution_summary["selected_models"]:
                hist = scoring_history_store.get_history(model_id=m, limit=1)
                if hist:
                    execution_summary["quality_score"] = round(hist[0].final_score, 2)
                    break
        except Exception:
            pass

        return _render_studio_response(
            messages=render_messages,
            conversation=conversation,
            mode=mode,
            execution=execution_summary,
            status="success",
        )

    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        execution_summary["duration_ms"] = round(elapsed_ms, 1)
        execution_summary["status"] = "error"
        execution_summary["error"] = str(exc)

        logger.exception("studio_chat_error", request_id=request_id, error=str(exc))

        # Remove the user message since we couldn't respond
        conversation.pop()

        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution=execution_summary,
            status="error",
            error_message=str(exc),
        )


@router.get("/studio/executions/{execution_id}")
async def studio_execution_detail(execution_id: str):
    """Get user-facing execution details for expandable details view.

    Returns structured data suitable for the /studio details panel:
    - execution overview (mode, duration, quality, fallbacks)
    - per-stage breakdown with human-readable explanations
    - fallback chain with user-friendly reasons
    - cross-stage quality summary
    """
    from app.pipeline.observability.store import execution_store

    summary = execution_store.get(execution_id)
    if summary is None:
        # Try persistent store
        try:
            from app.pipeline.observability.persistent_store import get_persistent_store
            persistent = get_persistent_store()
            data = persistent.get(execution_id)
            if data is None:
                return {"error": f"Execution '{execution_id}' not found"}
            data = _build_user_facing_details(data)
            return data
        except Exception:
            return {"error": f"Execution '{execution_id}' not found"}

    return _build_user_facing_details(summary.to_dict())


def _build_user_facing_details(data: dict) -> dict:
    """Transform raw execution data into user-facing structured details.

    Converts admin/debug format to product-facing format with
    human-readable stage explanations, simplified fallback reasons,
    and quality summaries.
    """
    stages = data.get("stages", data.get("stage_summaries", []))
    total_fallbacks = data.get("total_fallbacks", 0)
    total_retries = data.get("total_retries", 0)

    # Build user-facing stage cards
    user_stages = []
    for s in stages:
        role = s.get("stage_role", s.get("role", "unknown"))
        status = s.get("status", "unknown")
        selection = s.get("selection_explain", {})
        fallback_chain = selection.get("fallback_chain", []) if selection else []
        fb_count = s.get("fallback_count", 0)

        # Human-readable stage explanation
        explanation = _stage_explanation(role, status, fb_count)

        # Fallback details (user-friendly)
        fallbacks = []
        for fb in fallback_chain:
            fallbacks.append({
                "from_model": fb.get("failed_model", fb.get("model", "unknown")),
                "to_model": "",  # Will be filled by next candidate
                "reason": _user_friendly_fallback_reason(fb.get("reason", "")),
            })

        # Quality info
        quality_score = s.get("quality_score")
        quality_label = ""
        if quality_score and quality_score > 0:
            if quality_score >= 0.7:
                quality_label = "High confidence"
            elif quality_score >= 0.5:
                quality_label = "Moderate confidence"
            else:
                quality_label = "Low confidence"

        user_stages.append({
            "stage_id": s.get("stage_id", ""),
            "role": role,
            "role_label": _role_label(role),
            "model": selection.get("selected_model", s.get("selected_model", "")) if selection else s.get("selected_model", ""),
            "provider": selection.get("selected_provider", s.get("selected_provider", "")) if selection else s.get("selected_provider", ""),
            "transport": selection.get("selected_transport", s.get("selected_transport", "")) if selection else s.get("selected_transport", ""),
            "duration_ms": s.get("duration_ms", 0),
            "status": status,
            "status_label": "Completed" if status == "completed" else "Skipped" if status == "skipped" else "Failed",
            "fallback_count": fb_count,
            "fallbacks": fallbacks,
            "explanation": explanation,
            "quality_score": quality_score,
            "quality_label": quality_label,
            "retry_count": s.get("retry_count", 0),
        })

    # Fill in "to_model" for fallback chains
    for _i, stage in enumerate(user_stages):
        for j, fb in enumerate(stage["fallbacks"]):
            # The "to_model" is either the next fallback's from_model or the selected model
            if j + 1 < len(stage["fallbacks"]):
                fb["to_model"] = stage["fallbacks"][j + 1]["from_model"]
            elif stage["model"]:
                fb["to_model"] = stage["model"]

    # Cross-stage summary
    cross_stage = _extract_cross_stage(stages)

    # Overall verdict
    verdict = _compute_verdict(user_stages, total_fallbacks, data)

    return {
        "execution_id": data.get("execution_id", ""),
        "mode": data.get("mode", ""),
        "pipeline_id": data.get("pipeline_id", ""),
        "pipeline_display_name": data.get("pipeline_display_name", ""),
        "status": data.get("status", "unknown"),
        "duration_ms": data.get("duration_ms", 0),
        "stage_count": len(user_stages),
        "total_fallbacks": total_fallbacks,
        "total_retries": total_retries,
        "quality_score": data.get("quality_score", 0),
        "stages": user_stages,
        "cross_stage": cross_stage,
        "verdict": verdict,
    }


# ── Helpers ──


def _render_studio_response(
    messages: list[dict] | None = None,
    conversation: list[dict] | None = None,
    mode: str = "balanced",
    execution: dict | None = None,
    status: str = "success",
    error_message: str = "",
) -> HTMLResponse:
    """Render the studio chat response with OOB swaps.

    Updates:
    - #studio-messages — message list
    - #studio-execution-summary — right panel
    - #studio-conversation-input — hidden conversation state
    - .studio-response-state — hidden status div
    """
    mode_config = _resolve_mode(mode)

    html = _render_template(
        "partials/studio_response.html",
        messages=messages or [],
        conversation=conversation or [],
        mode=mode,
        mode_label=mode_config["label"],
        execution=execution or {},
        execution_id=execution.get("execution_id", "") if execution else "",
        status=status,
        error_message=error_message,
    )
    return HTMLResponse(content=html)


def _build_render_messages(conversation: list[dict]) -> list[dict]:
    """Convert conversation to render-ready message dicts with HTML content."""
    result = []
    for msg in conversation:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        ts = msg.get("timestamp", 0)

        if role == "assistant":
            html_content = render_markdown(content)
        else:
            html_content = bleach.clean(content, tags=[], attributes={})

        result.append({
            "role": role,
            "content": content,
            "html_content": html_content,
            "timestamp": ts,
            "is_error": False,
        })

    return result


def _safe_parse_json(text: str) -> list[dict]:
    """Safely parse JSON, returning empty list on failure."""
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, ValueError):
        return []


# ── User-facing detail helpers ──

# Role labels for user display
_ROLE_LABELS: dict[str, str] = {
    "generate": "Draft",
    "review": "Review",
    "critique": "Critique",
    "refine": "Refine",
    "regenerate": "Regenerate",
    "verify": "Verify",
    "finalize": "Finalize",
    "initial": "Initial draft",
    "transform": "Transform",
    "draft": "Draft",
}

# Stage explanations — product-friendly descriptions
_STAGE_EXPLANATIONS: dict[str, str] = {
    "generate": "Draft generated",
    "review": "Answer reviewed",
    "critique": "Deep critique completed",
    "refine": "Final version refined",
    "regenerate": "Answer regenerated with new perspective",
    "verify": "Facts verified",
    "finalize": "Final version prepared",
    "initial": "Initial draft created",
    "transform": "Response transformed",
    "draft": "Draft created",
}


def _role_label(role: str) -> str:
    """Return a user-friendly label for a stage role."""
    return _ROLE_LABELS.get(role, role.capitalize())


def _stage_explanation(role: str, status: str, fallback_count: int) -> str:
    """Return a product-friendly explanation for what the stage did."""
    base = _STAGE_EXPLANATIONS.get(role, f"{role.capitalize()} completed")

    if status == "skipped":
        return f"{base} (skipped)"
    if status == "failed":
        return f"{base} — failed"
    if fallback_count > 0:
        return f"{base} (with fallback)"
    return base


def _user_friendly_fallback_reason(raw_reason: str) -> str:
    """Convert technical fallback reason to user-friendly text."""
    reason_lower = raw_reason.lower() if raw_reason else ""

    if "timeout" in reason_lower or "timed out" in reason_lower:
        return "timed out"
    if "circuit" in reason_lower:
        return "temporarily unavailable"
    if "unavailable" in reason_lower or "not available" in reason_lower:
        return "unavailable"
    if "rate" in reason_lower:
        return "rate limited"
    if "degrad" in reason_lower:
        return "degraded"
    if "error" in reason_lower:
        return "error occurred"
    if "not found" in reason_lower:
        return "not found"

    return raw_reason if raw_reason else "switched model"


def _extract_cross_stage(stages: list[dict]) -> dict:
    """Extract cross-stage quality signals from stage data."""
    result: dict[str, Any] = {}

    for s in stages:
        cross = s.get("cross_stage", {})
        if not cross:
            continue

        role = s.get("stage_role", s.get("role", ""))

        if role == "generate":
            dc = cross.get("downstream_corrections", 0)
            cs = cross.get("correction_severity", 0)
            result["generate"] = {
                "downstream_corrections": dc,
                "correction_severity": round(cs, 2),
                "final_improvement_score": cross.get("final_improvement_score", 0),
            }
            if dc > 0:
                severity_text = "high" if cs > 0.6 else "moderate" if cs > 0.3 else "minor"
                result["generate"]["summary"] = f"{dc} issues found by review ({severity_text})"
            else:
                result["generate"]["summary"] = "No issues found by review"

        elif role in ("review", "critique"):
            ra = cross.get("review_actionability", 0.5)
            result["review"] = {
                "actionability": round(ra, 2),
            }
            if ra >= 0.7:
                result["review"]["summary"] = "Review findings were acted on"
            elif ra >= 0.3:
                result["review"]["summary"] = "Some review findings were addressed"
            else:
                result["review"]["summary"] = "Review had limited impact"

        elif role == "refine":
            re_score = cross.get("refine_effectiveness", 0.5)
            result["refine"] = {
                "effectiveness": round(re_score, 2),
            }
            if re_score >= 0.7:
                result["refine"]["summary"] = "Refinement was effective"
            elif re_score >= 0.4:
                result["refine"]["summary"] = "Moderate refinement achieved"
            else:
                result["refine"]["summary"] = "Limited refinement impact"

    return result


def _compute_verdict(stages: list[dict], total_fallbacks: int, data: dict) -> dict:
    """Compute an overall verdict for the execution."""
    all_completed = all(s["status"] == "completed" for s in stages)
    has_fallbacks = total_fallbacks > 0

    # Average quality across stages
    quality_scores = [s["quality_score"] for s in stages if s.get("quality_score") and s["quality_score"] > 0]
    avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0

    # Determine verdict label and message
    if not all_completed:
        label = "Completed with issues"
        message = "Some stages did not complete successfully"
    elif has_fallbacks:
        label = "Completed with fallbacks"
        message = f"Model fallbacks were used ({total_fallbacks} times)"
    elif avg_quality >= 0.7:
        label = "High confidence result"
        message = "Response generated with high quality signals"
    elif avg_quality >= 0.5:
        label = "Good result"
        message = "Response completed successfully"
    else:
        label = "Result with low confidence"
        message = "Response completed but quality signals are low"

    return {
        "label": label,
        "message": message,
        "avg_quality": round(avg_quality, 2) if avg_quality else None,
        "all_completed": all_completed,
        "has_fallbacks": has_fallbacks,
    }
