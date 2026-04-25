"""
Model definitions for the API.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.config import get_config


# Default model list
DEFAULT_MODELS = [
    {"id": "gpt-5-3", "upstream": "gpt-5-3", "type": "chat"},
    {"id": "gpt-5-2", "upstream": "gpt-5-2", "type": "chat"},
    {"id": "gpt-5-1", "upstream": "gpt-5-1", "type": "chat"},
    {"id": "gpt-5", "upstream": "gpt-5", "type": "chat"},
    {"id": "gpt-5-mini", "upstream": "gpt-5-mini", "type": "chat"},
    {"id": "gpt-5-3-mini", "upstream": "gpt-5-3-mini", "type": "chat"},
    {"id": "gpt-5-4-t-mini", "upstream": "gpt-5-4-t-mini", "type": "chat"},
    {"id": "auto", "upstream": "auto", "type": "chat"},
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
