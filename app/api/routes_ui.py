import json
import os
import time

import bleach
import jinja2
import markdown
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.core.logging import get_logger
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.services.chat_proxy_service import service
from app.services.model_registry_service import service as model_service

logger = get_logger(__name__)

router = APIRouter()

_templates_env: jinja2.Environment | None = None


def get_jinja_env() -> jinja2.Environment:
    global _templates_env
    if _templates_env is None:
        template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
        _templates_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_dir),
            autoescape=True,
        )
    return _templates_env


def render_template(template_name: str, context: dict) -> str:
    template = get_jinja_env().get_template(template_name)
    return template.render(**context)


def render_markdown(content: str) -> str:
    html = markdown.markdown(
        content,
        extensions=["fenced_code", "tables"],
    )
    allowed_tags = list(bleach.sanitizer.ALLOWED_TAGS) + [
        "p",
        "br",
        "strong",
        "em",
        "code",
        "pre",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
    ]
    allowed_attrs = {**bleach.sanitizer.ALLOWED_ATTRIBUTES, "code": ["class"], "pre": ["class"]}
    return bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs)


@router.get("/ui", response_class=HTMLResponse)
async def ui_index(request: Request):
    browser_models, api_models = model_service.group_models()

    html = render_template(
        "index.html",
        {
            "request": request,
            "browser_models": browser_models,
            "api_models": api_models,
            "selected_model": "",
            "selected_transport": "",
            "provider_id": "",
            "messages": [],
            "conversation_json": "[]",
            "last_response": "",
            "last_duration": None,
            "usage": None,
            "last_status": "",
            "error_message": "",
            "search_query": "",
        },
    )
    return HTMLResponse(content=html)


@router.get("/ui/models", response_class=HTMLResponse)
async def ui_models(request: Request, q: str = "", selected: str = ""):
    if q:
        filtered = model_service.filter_models(q)
        browser_models = [m for m in filtered if m.transport == "browser"]
        api_models = [m for m in filtered if m.transport == "api"]
    else:
        browser_models, api_models = model_service.group_models()

    html = render_template(
        "partials/models_list.html",
        {
            "request": request,
            "browser_models": browser_models,
            "api_models": api_models,
            "selected_model": selected,
            "search_query": q,
        },
    )
    return HTMLResponse(content=html)


@router.get("/ui/diagnostics", response_class=HTMLResponse)
async def ui_diagnostics(request: Request, model: str = ""):
    if not model:
        html = render_template(
            "partials/diagnostics.html",
            {
                "request": request,
                "selected_model": "",
                "selected_transport": "",
                "provider_id": "",
                "last_duration": None,
                "usage": None,
                "last_status": "",
                "error_message": "",
            },
        )
        return HTMLResponse(content=html)

    resolved = resolve_model_for_diagnostics(model)

    html = render_template(
        "partials/diagnostics.html",
        {
            "request": request,
            "selected_model": model,
            "selected_transport": resolved["transport"],
            "provider_id": resolved["provider_id"],
            "last_duration": None,
            "usage": None,
            "last_status": "",
            "error_message": "",
        },
    )
    return HTMLResponse(content=html)


def resolve_model_for_diagnostics(model: str) -> dict:
    try:
        from app.registry.unified import unified_registry

        resolved = unified_registry.resolve_model(model)
        return {
            "transport": resolved.transport,
            "provider_id": resolved.provider_id,
        }
    except Exception:
        return {
            "transport": "unknown",
            "provider_id": "unknown",
        }


model_service.resolve_model_for_diagnostics = resolve_model_for_diagnostics


@router.post("/ui/chat", response_class=HTMLResponse)
async def ui_chat(
    request: Request,
    model: str = Form(""),
    message: str = Form(""),
    conversation_json: str = Form("[]"),
    action: str = Form(""),
):
    if action == "clear":
        html = render_template(
            "partials/chat_response.html",
            {
                "request": request,
                "messages": [],
                "last_response": "",
                "last_duration": None,
                "usage": None,
                "last_status": "",
                "error_message": "",
                "selected_model": model,
                "selected_transport": "",
                "provider_id": "",
            },
        )
        return HTMLResponse(content=html)

    if not model or not message:
        html = render_template(
            "partials/chat_response.html",
            {
                "request": request,
                "messages": [],
                "last_response": "",
                "last_duration": None,
                "usage": None,
                "last_status": "error",
                "error_message": "Model and message are required",
                "selected_model": model,
                "selected_transport": "",
                "provider_id": "",
            },
        )
        return HTMLResponse(content=html)

    try:
        messages = json.loads(conversation_json) if conversation_json else []
        messages.append({"role": "user", "content": message})

        chat_request = ChatCompletionRequest(
            model=model,
            messages=[ChatMessage(role=m["role"], content=m["content"]) for m in messages],
            stream=False,
        )

        start_time = time.monotonic()
        response = await service.process_completion(chat_request, request_id="ui-session")
        duration = time.monotonic() - start_time

        assistant_content = response.choices[0].message.content if response.choices else ""
        assistant_content_html = render_markdown(assistant_content)

        messages.append(
            {
                "role": "assistant",
                "content": assistant_content,
                "timestamp": time.strftime("%H:%M"),
            }
        )

        resolved = resolve_model_for_diagnostics(model)

        html = render_template(
            "partials/chat_response.html",
            {
                "request": request,
                "messages": messages,
                "last_response": assistant_content_html,
                "last_duration": duration,
                "usage": response.usage if response.usage else None,
                "last_status": "success",
                "error_message": "",
                "selected_model": model,
                "selected_transport": resolved["transport"],
                "provider_id": resolved["provider_id"],
            },
        )
        return HTMLResponse(content=html)

    except Exception as e:
        logger.exception("UI chat error", error=str(e))
        html = render_template(
            "partials/chat_response.html",
            {
                "request": request,
                "messages": [],
                "last_response": "",
                "last_duration": None,
                "usage": None,
                "last_status": "error",
                "error_message": str(e),
                "selected_model": model,
                "selected_transport": "",
                "provider_id": "",
            },
        )
        return HTMLResponse(content=html)
