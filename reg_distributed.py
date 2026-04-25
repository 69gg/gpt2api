#!/usr/bin/env python3
"""
Distributed registration script — runs independently and pushes new tokens
to specified gpt2api instances.

Usage:
    python reg_distributed.py \
        --cf-url https://xxx --cf-auth xxx \
        --instances "http://host1:8000,http://host2:8000" \
        --count 5

Instances are comma-separated URLs of gpt2api deployments.
After successful registration, the token is immediately POSTed to
/admin/tokens on each instance with the admin_key header.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from curl_cffi import requests as curl_requests

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from app.reg_web import (
    register_account, CFEmailProvider, FlowContext, BrowserFingerprint,
    _normalize_proxy_url, _log, WEB_TOKEN_DIR,
)


def push_token_to_instance(instance_url: str, token_data: dict, admin_key: str) -> bool:
    """Push a single token to a gpt2api instance via /admin/tokens."""
    headers = {
        "Content-Type": "application/json",
        "X-Admin-Key": admin_key,
    }
    try:
        resp = curl_requests.post(
            f"{instance_url.rstrip('/')}/admin/tokens",
            json=token_data,
            headers=headers,
            timeout=30,
            impersonate="chrome136",
        )
        if resp.status_code == 200:
            _log(f"  Pushed to {instance_url} OK")
            return True
        _log(f"  Push to {instance_url} failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        _log(f"  Push to {instance_url} error: {e}")
    return False


def main():
    parser = argparse.ArgumentParser(description="Distributed token registration")
    parser.add_argument("--cf-url", required=True, help="Cloudflare email service URL")
    parser.add_argument("--cf-auth", required=True, help="CF service auth token")
    parser.add_argument("--cf-admin-auth", default="", help="CF admin auth token")
    parser.add_argument("--cf-domain", default="", help="Email domain override")
    parser.add_argument("--proxy", default="", help="Proxy for registration (and token default)")
    parser.add_argument("--instances", required=True, help="Comma-separated gpt2api instance URLs")
    parser.add_argument("--admin-key", default="admin-gpt2api", help="Admin key for pushing tokens")
    parser.add_argument("--count", type=int, default=1, help="Number of accounts to register")
    parser.add_argument("--save-local", action="store_true", help="Also save token JSON locally")
    args = parser.parse_args()

    instances = [u.strip() for u in args.instances.split(",") if u.strip()]
    if not instances:
        _log("No instances specified")
        sys.exit(1)

    proxy = args.proxy
    proxies = None
    if proxy:
        try:
            proxy_url = _normalize_proxy_url(proxy)
            proxies = {"http": proxy_url, "https": proxy_url}
        except ValueError as e:
            _log(f"Invalid proxy: {e}")
            sys.exit(1)

    email_provider = CFEmailProvider(
        cf_url=args.cf_url,
        cf_auth=args.cf_auth,
        cf_admin_auth=args.cf_admin_auth,
        cf_domain=args.cf_domain,
        proxies=proxies,
    )

    success = 0
    for i in range(args.count):
        _log(f"[{i+1}/{args.count}] Registering account...")
        try:
            fp = BrowserFingerprint.chrome_windows()
            session = curl_requests.Session(impersonate=fp.impersonate, proxies=proxies)
            from app.reg_web import _browser_identity_headers
            session.headers.update(_browser_identity_headers(fp.user_agent, fp=fp))

            context = FlowContext(
                fingerprint=fp,
                redirect_uri="https://platform.openai.com/auth/callback",
                client_id="app_2SKx67EdpoN0G6j64rFvigXD",
            )

            token_data = register_account(session, context, email_provider, proxies=proxies, proxy=proxy)
            _log(f"  Registered: {token_data.get('email')}")

            # Add proxy to token data
            token_data["proxy"] = proxy

            # Save locally if requested
            if args.save_local:
                WEB_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
                email_safe = token_data["email"].replace(".", "_").replace("@", "_at_")
                path = WEB_TOKEN_DIR / f"{email_safe}.json"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(token_data, f, indent=2, ensure_ascii=False)
                _log(f"  Saved to {path.name}")

            # Push to all instances
            for inst in instances:
                push_token_to_instance(inst, token_data, args.admin_key)

            success += 1
        except Exception as e:
            _log(f"  Registration failed: {e}")

        if i < args.count - 1:
            time.sleep(5)

    _log(f"Done: {success}/{args.count} registered and pushed")


if __name__ == "__main__":
    main()
