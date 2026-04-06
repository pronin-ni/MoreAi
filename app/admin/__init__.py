"""
Admin control plane — runtime configuration management.

Layered config model:
  BaseSettings (from .env) → RuntimeOverrides (admin.json) → EffectiveConfig (computed)
"""

from app.admin.config_manager import config_manager
from app.admin.observer import observer

__all__ = ["config_manager", "observer"]
