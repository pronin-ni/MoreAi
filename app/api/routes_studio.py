"""
Studio UI routes.

Provides a product-facing "smart chat" interface with pipeline awareness.
Separate from /ui — /ui remains the simple HTMX chat.

Studio modes are POLICY-driven, not model-driven:
- Fast/Balanced use the intelligence layer to select the best available
  candidate at runtime (availability, latency, stability, quality).
- Quality/Review/Deep use pipelines with dynamic stage selection.
"""

from __future__ import annotations

import json
import time
import uuid

import bleach
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.api.routes_ui import render_markdown
from app.api.studio_modes import STUDIO_MODE_POLICIES, get_mode_policy, get_selection_policy
from app.core.logging import get_logger
from app.schemas.openai import ChatCompletionRequest, ChatMessage

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


# ── Routes ──


@router.get("/studio")
async def studio_page():
    """Render the Studio UI page."""
    from app.pipeline.types import pipeline_registry
    from app.services.model_registry_service import service as model_registry_service

    # Get available modes
    modes = []
    for mode_key, mode_config in STUDIO_MODE_POLICIES.items():
        is_pipeline = mode_config.get("is_pipeline", False)
        available = True
        if is_pipeline:
            p_def = pipeline_registry.get(mode_config.get("pipeline_id", ""))
            available = p_def is not None and p_def.enabled if p_def else False

        modes.append(
            {
                "key": mode_key,
                "label": mode_config["label"],
                "description": mode_config["description"],
                "available": available,
                "is_pipeline": is_pipeline,
            }
        )

    # Get grouped models for advanced mode
    browser_models, api_models, agent_models = model_registry_service.group_models()

    return HTMLResponse(
        content=_render_template(
            "studio.html",
            modes=modes,
            default_mode="balanced",
            browser_models=browser_models,
            api_models=api_models,
            agent_models=agent_models,
            STUDIO_MODES={
                k: {"label": v["label"], "isPipeline": v["is_pipeline"]}
                for k, v in STUDIO_MODE_POLICIES.items()
            },
        )
    )


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

    For non-pipeline modes: uses the intelligence layer to select the
    best available candidate at runtime, with bounded fallback.
    For pipeline modes: executes the pipeline with dynamic stage selection.
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

    # Append user message BEFORE dispatching to execution
    user_msg = {
        "role": "user",
        "content": message,
        "timestamp": time.time(),
    }
    conversation.append(user_msg)

    # Resolve mode to backend strategy
    policy = get_mode_policy(mode)
    is_pipeline = policy.get("is_pipeline", False)
    pipeline_id = policy.get("pipeline_id", "")

    # Advanced mode: user selected specific model (explicit override)
    if advanced_type == "custom_model" and advanced_model:
        return await _execute_studio_single_model(
            conversation,
            advanced_model,
            mode,
            request,
        )

    if is_pipeline:
        return await _execute_studio_pipeline(
            conversation,
            pipeline_id,
            mode,
            request,
        )

    # Single-model mode: use intelligence layer for selection
    return await _execute_studio_intelligent(
        conversation,
        mode,
        request,
    )


async def _execute_studio_intelligent(
    conversation: list[dict],
    mode: str,
    request: Request,
) -> HTMLResponse:
    """Execute using intelligence-layer candidate selection.

    Uses ModelSelector to rank ALL available models by the mode's
    SelectionPolicy, picks the best, and falls back on failure.
    """
    from app.intelligence.selection import model_selector
    from app.intelligence.types import SelectionPolicy, StageRole

    # Get mode's selection policy
    sel_policy_dict = get_selection_policy(mode)
    if not sel_policy_dict:
        # Fallback to balanced defaults
        sel_policy_dict = {
            "preferred_models": [],
            "avoid_tags": [],
            "min_availability": 0.3,
            "max_latency_s": 60.0,
            "fallback_mode": "next_best",
            "max_fallback_attempts": 3,
        }

    sel_policy = SelectionPolicy(**sel_policy_dict)

    # Use ModelSelector to rank all candidates for "generate" role
    selection_trace = model_selector.select_for_stage(
        stage_id="studio",
        stage_role=StageRole.GENERATE,
        policy=sel_policy,
    )

    # Extract ranked candidates (exclude excluded ones for primary)
    viable = [c for c in selection_trace.all_candidates if not c.is_excluded]
    if not viable:
        # Fall back to including excluded candidates as last resort
        viable = selection_trace.all_candidates

    if not viable:
        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution={
                "status": "error",
                "error": "No viable candidates available",
                "execution_type": "model",
                "target_model": "",
                "used_model": "",
                "used_provider": "",
                "stage_count": 0,
                "fallback_count": 0,
                "quality_score": 0.0,
                "duration_ms": 0,
            },
            status="error",
            error_message="No models available. Check provider health in Admin.",
        )

    # Build messages
    messages = [ChatMessage(**m) for m in conversation if m.get("role") in ("user", "assistant")]

    request_id = f"studio-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()

    # Try candidates with bounded fallback
    max_attempts = sel_policy.max_fallback_attempts
    response = None
    fallback_count = 0
    fallback_records: list[dict] = []
    last_error = None
    used_model = ""
    used_provider = ""
    excluded_ids: set[str] = set()

    for attempt_idx, candidate in enumerate(viable):
        if attempt_idx > max_attempts:
            break

        # Skip already-excluded candidates
        if candidate.model_id in excluded_ids:
            continue

        chat_request = ChatCompletionRequest(
            model=candidate.model_id,
            messages=messages,
            stream=False,
        )

        try:
            from app.services.chat_proxy_service import service as proxy_service

            response = await proxy_service.process_completion(chat_request, request_id)
            used_model = candidate.model_id
            used_provider = candidate.provider_id
            break

        except Exception as exc:
            last_error = exc
            error_msg = str(exc)
            failure_reason = _classify_fallback_reason(error_msg)

            # Determine if this is a terminal failure (candidate should be excluded)
            is_terminal = _is_terminal_studio_failure(exc)

            if is_terminal:
                excluded_ids.add(candidate.model_id)
                logger.warning(
                    "studio_intelligent_candidate_excluded",
                    request_id=request_id,
                    candidate=candidate.model_id,
                    provider=candidate.provider_id,
                    score=round(candidate.final_score, 3),
                    reason=failure_reason,
                    error=error_msg[:200],
                    total_excluded=str(len(excluded_ids)),
                )
            else:
                logger.warning(
                    "studio_intelligent_candidate_failed",
                    request_id=request_id,
                    candidate=candidate.model_id,
                    provider=candidate.provider_id,
                    score=round(candidate.final_score, 3),
                    reason=failure_reason,
                    error=error_msg[:200],
                )

            fallback_records.append(
                {
                    "failed_model": candidate.model_id,
                    "failed_provider": candidate.provider_id,
                    "score": round(candidate.final_score, 3),
                    "reason": failure_reason,
                    "is_terminal": is_terminal,
                }
            )

            if attempt_idx + 1 <= max_attempts:
                fallback_count += 1
                continue
            else:
                break

    elapsed_ms = (time.monotonic() - started) * 1000

    # ── Build execution summary ──
    execution_summary = _build_intelligent_execution_summary(
        mode,
        sel_policy_dict,
        viable[0] if viable else None,
        used_model,
        used_provider,
        fallback_count,
        fallback_records,
        elapsed_ms,
        response,
        conversation,
        request_id,
    )

    if response:
        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution=execution_summary,
            status="success",
        )

    # All candidates failed
    error_text = str(last_error) if last_error else "Unknown error"
    error_summary = error_text[:300] if len(error_text) > 300 else error_text
    candidate_list = ", ".join(c.model_id for c in viable[:5])

    logger.exception(
        "studio_intelligent_all_candidates_failed",
        request_id=request_id,
        candidates=candidate_list,
        fallback_count=fallback_count,
    )

    return _render_studio_response(
        messages=_build_render_messages(conversation),
        conversation=conversation,
        mode=mode,
        execution=execution_summary,
        status="error",
        error_message=f"All models unavailable. Tried: {candidate_list}. Error: {error_summary}",
    )


@router.get("/studio/executions/{execution_id}")
async def studio_execution_detail(execution_id: str):
    """Get user-facing execution details for expandable details view."""
    from app.pipeline.observability.store import execution_store

    summary = execution_store.get(execution_id)
    if summary is None:
        try:
            from app.pipeline.observability.persistent_store import get_persistent_store

            persistent = get_persistent_store()
            data = persistent.get(execution_id)
            if data is None:
                return {"error": f"Execution '{execution_id}' not found"}
            return _build_user_facing_details(data)
        except Exception:
            return {"error": f"Execution '{execution_id}' not found"}

    return _build_user_facing_details(summary.to_dict())


async def _execute_studio_single_model(
    conversation: list[dict],
    model_id: str,
    mode: str,
    request: Request,
) -> HTMLResponse:
    """Execute a specific model (advanced/manual mode)."""
    from app.services.chat_proxy_service import service as proxy_service

    messages = [ChatMessage(**m) for m in conversation if m.get("role") in ("user", "assistant")]
    chat_request = ChatCompletionRequest(model=model_id, messages=messages, stream=False)
    request_id = f"studio-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()

    try:
        response = await proxy_service.process_completion(chat_request, request_id)
        elapsed_ms = (time.monotonic() - started) * 1000
        content = response.choices[0].message.content if response.choices else ""
        conversation.append({"role": "assistant", "content": content, "timestamp": time.time()})

        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution={
                "execution_type": "model",
                "target_model": model_id,
                "used_model": model_id,
                "used_provider": getattr(response, "_provider", ""),
                "fallback_count": 0,
                "quality_score": 0.0,
                "duration_ms": round(elapsed_ms, 1),
                "status": "success",
            },
            status="success",
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.exception("studio_single_model_failed", model=model_id)
        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution={
                "execution_type": "model",
                "target_model": model_id,
                "used_model": model_id,
                "used_provider": "",
                "fallback_count": 0,
                "quality_score": 0.0,
                "duration_ms": round(elapsed_ms, 1),
                "status": "error",
                "error": str(exc)[:300],
            },
            status="error",
            error_message=f"Model {model_id} failed: {str(exc)[:200]}",
        )


async def _execute_studio_pipeline(
    conversation: list[dict],
    pipeline_id: str,
    mode: str,
    request: Request,
) -> HTMLResponse:
    """Execute a pipeline (stages use dynamic selection)."""
    from app.pipeline.executor import pipeline_executor
    from app.pipeline.types import pipeline_registry

    messages = [ChatMessage(**m) for m in conversation if m.get("role") in ("user", "assistant")]

    if not messages:
        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution={
                "status": "error",
                "error": "No user messages in conversation",
                "execution_type": "model",
                "target_model": "",
                "used_model": "",
                "used_provider": "",
                "stage_count": 0,
                "fallback_count": 0,
                "quality_score": 0.0,
                "duration_ms": 0,
            },
            status="error",
            error_message="Conversation is empty. Please send a message.",
        )

    chat_request = ChatCompletionRequest(
        model=f"pipeline/{pipeline_id}", messages=messages, stream=False
    )
    request_id = f"studio-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()

    p_def = pipeline_registry.get(pipeline_id)
    if p_def is None:
        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution={
                "status": "error",
                "error": f"Pipeline '{pipeline_id}' not found",
                "execution_type": "pipeline",
                "target_model": f"pipeline/{pipeline_id}",
                "used_model": f"pipeline/{pipeline_id}",
                "used_provider": "",
                "stage_count": 0,
                "fallback_count": 0,
                "quality_score": 0.0,
                "duration_ms": 0,
            },
            status="error",
            error_message=f"Pipeline '{pipeline_id}' not found",
        )

    try:
        response = await pipeline_executor.execute(p_def, chat_request, request_id)
        elapsed_ms = (time.monotonic() - started) * 1000
        content = response.choices[0].message.content if response.choices else ""
        conversation.append({"role": "assistant", "content": content, "timestamp": time.time()})

        # Extract execution_id from response.id (format: "pipeline-{trace_id}")
        exec_id = (
            response.id.replace("pipeline-", "", 1) if response.id.startswith("pipeline-") else ""
        )

        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution={
                "execution_id": exec_id,
                "execution_type": "pipeline",
                "target_model": f"pipeline/{pipeline_id}",
                "used_model": f"pipeline/{pipeline_id}",
                "used_provider": getattr(response, "_provider", ""),
                "pipeline_id": pipeline_id,
                "stage_count": len(p_def.stages),
                "fallback_count": 0,
                "quality_score": 0.0,
                "duration_ms": round(elapsed_ms, 1),
                "status": "success",
            },
            status="success",
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.exception("studio_pipeline_failed", pipeline_id=pipeline_id)
        return _render_studio_response(
            messages=_build_render_messages(conversation),
            conversation=conversation,
            mode=mode,
            execution={
                "execution_type": "pipeline",
                "target_model": f"pipeline/{pipeline_id}",
                "used_model": f"pipeline/{pipeline_id}",
                "used_provider": "",
                "pipeline_id": pipeline_id,
                "stage_count": len(p_def.stages),
                "fallback_count": 0,
                "quality_score": 0.0,
                "duration_ms": round(elapsed_ms, 1),
                "status": "error",
                "error": str(exc)[:300],
            },
            status="error",
            error_message=f"Pipeline '{pipeline_id}' failed: {str(exc)[:200]}",
        )


# ── Helper functions ──


def _build_intelligent_execution_summary(
    mode: str,
    policy_dict: dict,
    top_candidate,
    used_model: str,
    used_provider: str,
    fallback_count: int,
    fallback_records: list[dict],
    elapsed_ms: float,
    response,
    conversation: list[dict],
    request_id: str,
) -> dict:
    """Build execution summary for intelligent selection mode."""
    summary: dict = {
        "execution_type": "model",
        "mode_policy": mode,
        "target_model": top_candidate.model_id if top_candidate else "",
        "used_model": used_model,
        "used_provider": used_provider,
        "fallback_count": fallback_count,
        "fallback_records": fallback_records,
        "quality_score": 0.0,
        "duration_ms": round(elapsed_ms, 1),
        "status": "success" if response else "error",
        "error": "",
    }

    if response:
        # Try to get quality score
        try:
            from app.pipeline.observability.scoring_history import scoring_history_store

            for m in [used_model]:
                hist = scoring_history_store.get_history(model_id=m, limit=1)
                if hist:
                    summary["quality_score"] = round(hist[0].final_score, 2)
                    break
        except Exception:
            pass

        # Append assistant response
        content = response.choices[0].message.content if response.choices else ""
        conversation.append({"role": "assistant", "content": content, "timestamp": time.time()})

    return summary


def _classify_fallback_reason(error_msg: str) -> str:
    """Classify the reason for a fallback from the error message."""
    msg_lower = error_msg.lower()
    if "auth" in msg_lower or "credential" in msg_lower or "login" in msg_lower:
        return "auth_unavailable"
    if "circuit" in msg_lower or "unavailable" in msg_lower:
        return "provider_unavailable"
    if "timeout" in msg_lower:
        return "timeout"
    if "rate" in msg_lower or "429" in msg_lower:
        return "rate_limited"
    if "degrad" in msg_lower:
        return "degraded"
    return "execution_error"


def _is_terminal_studio_failure(exc: Exception) -> bool:
    """Determine if a studio candidate failure is terminal (should exclude candidate).

    Terminal failures mean this candidate cannot succeed:
    - ServiceUnavailableError / no available providers
    - available=false / excluded by availability filter
    - Missing auth / browser unavailable
    - Circuit breaker open
    - Model not found / misconfigured

    Retryable failures may succeed on retry:
    - Transient timeout
    - 429 / rate limit
    """
    from app.core.errors import ServiceUnavailableError

    error_type = type(exc).__name__.lower()
    error_msg = str(exc).lower()

    # Terminal: service/model unavailability
    terminal_indicators = [
        "no available",
        "no viable",
        "service_unavailable",
        "unavailable",
        "available=false",
        "circuit_breaker_open",
        "not found",
        "unknown model",
        "missing auth",
        "browser unavailable",
        "no candidates",
    ]

    for indicator in terminal_indicators:
        if indicator in error_msg or indicator in error_type:
            return True

    # ServiceUnavailableError is always terminal
    if isinstance(exc, ServiceUnavailableError):
        return True

    # Timeout and rate limits are potentially retryable
    return not (
        "timeout" in error_msg
        or "gateway_timeout" in error_type
        or "timed out" in error_msg
        or "rate" in error_msg
        or "429" in error_msg
    )


def _render_studio_response(
    messages: list[dict] | None = None,
    conversation: list[dict] | None = None,
    mode: str = "balanced",
    execution: dict | None = None,
    status: str = "success",
    error_message: str = "",
) -> HTMLResponse:
    """Render the studio chat response with OOB swaps."""
    policy = get_mode_policy(mode)
    mode_label = policy.get("label", mode)
    exec_data = execution or {}
    execution_id = exec_data.get("execution_id", "")

    html = _render_template(
        "partials/studio_response.html",
        messages=messages or [],
        conversation=conversation or [],
        mode=mode,
        mode_label=mode_label,
        execution=exec_data,
        execution_id=execution_id,
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

        result.append(
            {
                "role": role,
                "content": content,
                "html_content": html_content,
                "timestamp": ts,
                "is_error": False,
            }
        )

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
    except json.JSONDecodeError, ValueError:
        return []


# ── Execution detail helpers (for /studio/executions/{id}) ──


def _stage_explanation(role: str, status: str, fallback_count: int) -> str:
    """Return a product-friendly explanation for what the stage did."""
    labels = {
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
    base = labels.get(role, f"{role.capitalize()} completed")

    if status == "skipped":
        return f"{base} (skipped)"
    if status == "failed":
        return f"{base} — failed"
    if fallback_count > 0:
        return f"{base} (with fallback)"
    return base


def _role_label(role: str) -> str:
    """Return a user-friendly label for a stage role."""
    labels = {
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
    return labels.get(role, role.capitalize())


def _extract_cross_stage(stages: list[dict]) -> dict:
    """Extract cross-stage quality signals from stage data."""
    result: dict[str, object] = {}

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

    quality_scores = [
        s["quality_score"] for s in stages if s.get("quality_score") and s["quality_score"] > 0
    ]
    avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0

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


def _build_user_facing_details(data: dict) -> dict:
    """Transform raw execution data into user-facing structured details."""
    stages = data.get("stages", data.get("stage_summaries", []))
    total_fallbacks = data.get("total_fallbacks", 0)
    total_retries = data.get("total_retries", 0)

    user_stages = []
    for s in stages:
        role = s.get("stage_role", s.get("role", "unknown"))
        status = s.get("status", "unknown")
        selection = s.get("selection_explain", {})
        fallback_chain = selection.get("fallback_chain", []) if selection else []
        fb_count = s.get("fallback_count", 0)

        explanation = _stage_explanation(role, status, fb_count)

        fallbacks = []
        for fb in fallback_chain:
            fallbacks.append(
                {
                    "from_model": fb.get("failed_model", fb.get("model", "unknown")),
                    "to_model": "",
                    "reason": _classify_fallback_reason(fb.get("reason", "")),
                }
            )

        quality_score = s.get("quality_score")
        quality_label = ""
        if quality_score and quality_score > 0:
            if quality_score >= 0.7:
                quality_label = "High confidence"
            elif quality_score >= 0.5:
                quality_label = "Moderate confidence"
            else:
                quality_label = "Low confidence"

        user_stages.append(
            {
                "stage_id": s.get("stage_id", ""),
                "role": role,
                "role_label": _role_label(role),
                "model": selection.get("selected_model", s.get("selected_model", ""))
                if selection
                else s.get("selected_model", ""),
                "provider": selection.get("selected_provider", s.get("selected_provider", ""))
                if selection
                else s.get("selected_provider", ""),
                "transport": selection.get("selected_transport", s.get("selected_transport", ""))
                if selection
                else s.get("selected_transport", ""),
                "duration_ms": s.get("duration_ms", 0),
                "status": status,
                "status_label": "Completed"
                if status == "completed"
                else "Skipped"
                if status == "skipped"
                else "Failed",
                "fallback_count": fb_count,
                "fallbacks": fallbacks,
                "explanation": explanation,
                "quality_score": quality_score,
                "quality_label": quality_label,
                "retry_count": s.get("retry_count", 0),
            }
        )

    for _i, stage in enumerate(user_stages):
        for j, fb in enumerate(stage["fallbacks"]):
            if j + 1 < len(stage["fallbacks"]):
                fb["to_model"] = stage["fallbacks"][j + 1]["from_model"]
            elif stage["model"]:
                fb["to_model"] = stage["model"]

    cross_stage = _extract_cross_stage(stages)
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
