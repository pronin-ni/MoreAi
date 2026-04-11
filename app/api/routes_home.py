"""
Home / landing page for MoreAI.

Returns an HTML overview of the system with navigation to /studio, /ui, /admin,
model list, API examples, and feature descriptions.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

router = APIRouter()

_template_env = Environment(
    loader=FileSystemLoader("app/templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


@router.get("/")
async def home_page():
    """Render the home / landing page."""
    tmpl = _template_env.get_template("home.html")
    return HTMLResponse(content=tmpl.render())
