#!/usr/bin/env python3
"""
gpt2api — OpenAI-compatible API backed by ChatGPT Web Chat.

Usage:
    python main.py [--config config.yaml] [--host 0.0.0.0] [--port 8000]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))


def main():
    parser = argparse.ArgumentParser(description="gpt2api server")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"), help="Config file path")
    parser.add_argument("--host", default=None, help="Host to bind")
    parser.add_argument("--port", type=int, default=None, help="Port to bind")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    from app.config import load_config, get_config
    load_config(args.config)

    host = args.host or get_config("server.host", "0.0.0.0")
    port = args.port or get_config("server.port", 8000)

    import uvicorn
    uvicorn.run(
        "app.server:create_app",
        host=host,
        port=port,
        factory=True,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
