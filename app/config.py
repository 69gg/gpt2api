"""Configuration management — YAML + environment variable override."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


_DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "api_key": "sk-gpt2api",
        "admin_key": "admin-gpt2api",
        "deployment_url": "",
    },
    "register": {
        "cf_url": "",
        "cf_auth": "",
        "cf_admin_auth": "",
        "cf_domain": "",
        "proxy": "",
        "auto_register": False,
        "min_tokens": 3,
        "max_register_parallel": 1,
        "register_interval": 60,
    },
    "token": {
        "refresh_interval_hours": 2,
        "dead_retain_hours": 24,
        "cooling_reset_hours": 24,
        "fail_threshold": 5,
        "load_balance": "round-robin",
    },
    "chatgpt": {
        "proxy": "",
        "sse_timeout": 120,
        "pow_max_iter": 500000,
        "image_download_timeout": 60,
        "turnstile_solver_url": "",
    },
    "models": [
        {"id": "gpt-5.3", "upstream": "gpt-5-3"},
        {"id": "gpt-5.2", "upstream": "gpt-5-2"},
        {"id": "gpt-5.1", "upstream": "gpt-5-1"},
        {"id": "gpt-5", "upstream": "gpt-5"},
        {"id": "gpt-5-mini", "upstream": "gpt-5-mini"},
        {"id": "gpt-5.3-mini", "upstream": "gpt-5-3-mini"},
        {"id": "gpt-5.4-mini-thinking", "upstream": "gpt-5-4-t-mini"},
        {"id": "auto", "upstream": "auto"},
        {"id": "gpt-image-2", "upstream": "gpt-5-3", "type": "image"},
    ],
}

_cfg: Dict[str, Any] = {}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _env_override(cfg: Dict[str, Any]) -> Dict[str, Any]:
    prefix = "GPT2API_"
    overrides: Dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("_")
        d = overrides
        for p in path[:-1]:
            d = d.setdefault(p, {})
        raw = value
        if raw.lower() in ("true", "1", "yes"):
            raw = True
        elif raw.lower() in ("false", "0", "no"):
            raw = False
        else:
            try:
                raw = int(raw)
            except ValueError:
                try:
                    raw = float(raw)
                except ValueError:
                    pass
        d[path[-1]] = raw
    return _deep_merge(cfg, overrides) if overrides else cfg


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    global _cfg
    base = _DEFAULT_CONFIG.copy()
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        base = _deep_merge(base, file_cfg)
    _cfg = _env_override(base)
    return _cfg


def get_config(key: str = "", default: Any = None) -> Any:
    if not _cfg:
        load_config()
    if not key:
        return _cfg
    parts = key.split(".")
    node = _cfg
    for p in parts:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return default
    return node
