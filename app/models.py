"""
Model definitions for the API.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.config import get_config


# Default model list
DEFAULT_MODELS = [
    {"id": "gpt-5.3-codex", "upstream": "auto", "type": "chat"},
    {"id": "gpt-5.2-codex", "upstream": "auto", "type": "chat"},
    {"id": "gpt-5.1-codex", "upstream": "auto", "type": "chat"},
    {"id": "gpt-5-codex", "upstream": "auto", "type": "chat"},
    {"id": "o4-mini", "upstream": "o4-mini", "type": "chat"},
    {"id": "gpt-4o", "upstream": "auto", "type": "chat"},
    {"id": "gpt-4o-mini", "upstream": "auto", "type": "chat"},
    {"id": "gpt-image-2", "upstream": "gpt-5-3", "type": "image"},
]


def get_models() -> List[Dict[str, Any]]:
    """Get model list from config or defaults."""
    config_models = get_config("models", [])
    if not config_models:
        config_models = DEFAULT_MODELS

    result = []
    for m in config_models:
        model_id = m.get("id", "") if isinstance(m, dict) else str(m)
        result.append({
            "id": model_id,
            "object": "model",
            "created": 1700000000,
            "owned_by": "openai",
        })
    return result


def get_openai_models_response() -> Dict[str, Any]:
    """Get OpenAI-format /v1/models response."""
    return {
        "object": "list",
        "data": get_models(),
    }
