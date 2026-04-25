#!/usr/bin/env python3
"""
Distributed registration script — runs independently and pushes new tokens
to specified gpt2api instances.

Supports:
- Finite mode: --count N registers N accounts then exits
- Infinite mode: --count 0 (or --infinite) registers forever until stopped
- Retry on registration failure with configurable attempts and backoff
- Retry on push failure to each instance
- Separate proxy for registration (--reg-proxy) and token config (--token-proxy)

Usage:
    # Register 5 accounts
    python reg_distributed.py \
        --cf-url https://xxx --cf-auth xxx \
        --instances "http://host1:8000,http://host2:8000" \
        --count 5

    # Register forever (infinite loop)
    python reg_distributed.py \
        --cf-url https://xxx --cf-auth xxx \
        --instances "http://host1:8000" \
        --infinite

    # Different proxies: register via local, token uses remote SOCKS5
    python reg_distributed.py \
        --cf-url https://xxx --cf-auth xxx \
        --instances "http://host1:8000" \
        --reg-proxy http://127.0.0.1:7890 \
        --token-proxy socks5://user:pass@remote:1080

    # With retry and interval
    python reg_distributed.py \
        --cf-url https://xxx --cf-auth xxx \
        --instances "http://host1:8000" \
        --count 10 --retry 3 --retry-delay 10 --interval 30
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import List

from curl_cffi import requests as curl_requests

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from app.reg_web import (
    register_account, CFEmailProvider, FlowContext, BrowserFingerprint,
    _normalize_proxy_url, _log, WEB_TOKEN_DIR,
)

# Graceful shutdown
_shutdown = False

def _signal_handler(sig, frame):
    global _shutdown
    if _shutdown:
        _log("Force exit")
        sys.exit(1)
    _shutdown = True
    _log("Shutting down after current registration completes...")

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def push_token_to_instance(instance_url: str, token_data: dict,
                            admin_key: str, max_retries: int = 3,
                            retry_delay: float = 5.0) -> bool:
    """Push a single token to a gpt2api instance with retry."""
    headers = {
        "Content-Type": "application/json",
        "X-Admin-Key": admin_key,
    }
    url = f"{instance_url.rstrip('/')}/admin/tokens"
    for attempt in range(1, max_retries + 1):
        if _shutdown:
            return False
        try:
            resp = curl_requests.post(
                url, json=token_data, headers=headers,
                timeout=30, impersonate="chrome136",
            )
            if resp.status_code == 200:
                _log(f"  Pushed to {instance_url} OK")
                return True
            if resp.status_code >= 500 and attempt < max_retries:
                _log(f"  Push to {instance_url} attempt {attempt}/{max_retries} failed: {resp.status_code}, retrying...")
                time.sleep(retry_delay * attempt)
                continue
            _log(f"  Push to {instance_url} failed: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            if attempt < max_retries:
                _log(f"  Push to {instance_url} attempt {attempt}/{max_retries} error: {e}, retrying...")
                time.sleep(retry_delay * attempt)
            else:
                _log(f"  Push to {instance_url} failed after {max_retries} attempts: {e}")
    return False


def push_to_all_instances(instances: List[str], token_data: dict,
                          admin_key: str, max_retries: int = 3,
                          retry_delay: float = 5.0) -> int:
    """Push token to all instances, return count of successful pushes."""
    ok = 0
    for inst in instances:
        if push_token_to_instance(inst, token_data, admin_key, max_retries, retry_delay):
            ok += 1
    return ok


def register_one(email_provider: CFEmailProvider, proxies: dict | None,
                  proxy: str, retry: int = 3, retry_delay: float = 10.0) -> dict | None:
    """Attempt to register a single account with retry."""
    for attempt in range(1, retry + 1):
        if _shutdown:
            return None
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
            token_data["proxy"] = token_proxy
            return token_data
        except Exception as e:
            if attempt < retry:
                wait = retry_delay * attempt
                _log(f"  Registration attempt {attempt}/{retry} failed: {e}, retrying in {wait:.0f}s...")
                # Interruptible sleep
                for _ in range(int(wait)):
                    if _shutdown:
                        return None
                    time.sleep(1)
            else:
                _log(f"  Registration failed after {retry} attempts: {e}")
    return None


def save_local(token_data: dict) -> None:
    """Save token JSON to local web_token directory."""
    WEB_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    email_safe = token_data["email"].replace(".", "_").replace("@", "_at_")
    path = WEB_TOKEN_DIR / f"{email_safe}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2, ensure_ascii=False)
    _log(f"  Saved to {path.name}")


def main():
    parser = argparse.ArgumentParser(description="Distributed token registration")
    parser.add_argument("--cf-url", required=True, help="Cloudflare email service URL")
    parser.add_argument("--cf-auth", required=True, help="CF service auth token")
    parser.add_argument("--cf-admin-auth", default="", help="CF admin auth token")
    parser.add_argument("--cf-domain", default="", help="Email domain override")
    parser.add_argument("--proxy", default="", help="Proxy for both registration and token (shorthand for --reg-proxy + --token-proxy)")
    parser.add_argument("--reg-proxy", default="", help="Proxy used only for registration requests")
    parser.add_argument("--token-proxy", default="", help="Proxy written into token JSON for gpt2api to use")
    parser.add_argument("--instances", required=True, help="Comma-separated gpt2api instance URLs")
    parser.add_argument("--admin-key", default="admin-gpt2api", help="Admin key for pushing tokens")
    parser.add_argument("--count", type=int, default=1, help="Number of accounts to register (0=infinite)")
    parser.add_argument("--infinite", action="store_true", help="Register forever (same as --count 0)")
    parser.add_argument("--interval", type=float, default=10.0, help="Seconds between registrations (default: 10)")
    parser.add_argument("--retry", type=int, default=3, help="Max retries per registration attempt (default: 3)")
    parser.add_argument("--retry-delay", type=float, default=10.0, help="Base delay between retries in seconds (default: 10)")
    parser.add_argument("--push-retry", type=int, default=3, help="Max retries for pushing to instance (default: 3)")
    parser.add_argument("--push-retry-delay", type=float, default=5.0, help="Base delay between push retries (default: 5)")
    parser.add_argument("--save-local", action="store_true", help="Also save token JSON locally")
    args = parser.parse_args()

    infinite = args.infinite or args.count == 0
    count = args.count if not infinite else 0

    instances = [u.strip() for u in args.instances.split(",") if u.strip()]
    if not instances:
        _log("No instances specified")
        sys.exit(1)

    # Resolve proxies: --proxy sets both, individual flags override
    reg_proxy = args.reg_proxy or args.proxy
    token_proxy = args.token_proxy or args.proxy
    reg_proxies = None
    if reg_proxy:
        try:
            proxy_url = _normalize_proxy_url(reg_proxy)
            reg_proxies = {"http": proxy_url, "https": proxy_url}
        except ValueError as e:
            _log(f"Invalid reg-proxy: {e}")
            sys.exit(1)
    if token_proxy:
        try:
            _normalize_proxy_url(token_proxy)
        except ValueError as e:
            _log(f"Invalid token-proxy: {e}")
            sys.exit(1)

    email_provider = CFEmailProvider(
        cf_url=args.cf_url,
        cf_auth=args.cf_auth,
        cf_admin_auth=args.cf_admin_auth,
        cf_domain=args.cf_domain,
        proxies=reg_proxies,
    )

    success = 0
    fail = 0
    i = 0

    _log(f"Starting: {'infinite' if infinite else f'count={count}'} mode, interval={args.interval}s, "
         f"retry={args.retry}, instances={len(instances)}")
    if reg_proxy:
        _log(f"  Registration proxy: {reg_proxy}")
    if token_proxy:
        _log(f"  Token proxy: {token_proxy}")

    while True:
        if _shutdown:
            break
        if not infinite and i >= count:
            break
        i += 1
        label = f"[{i}]" if infinite else f"[{i}/{count}]"
        _log(f"{label} Registering account...")

        token_data = register_one(
            email_provider, reg_proxies, reg_proxy,
            retry=args.retry, retry_delay=args.retry_delay,
        )

        if token_data is None:
            fail += 1
            if not infinite:
                continue
            # In infinite mode, back off on consecutive failures
            backoff = min(args.interval * (1 + fail * 0.5), 300)
            _log(f"  Backing off {backoff:.0f}s after {fail} consecutive failures")
            for _ in range(int(backoff)):
                if _shutdown:
                    break
                time.sleep(1)
            continue

        # Registration succeeded
        fail = 0
        success += 1
        email = token_data.get("email", "?")
        _log(f"  Registered: {email}")

        if args.save_local:
            save_local(token_data)

        # Push to all instances with retry
        pushed = push_to_all_instances(
            instances, token_data, args.admin_key,
            max_retries=args.push_retry, retry_delay=args.push_retry_delay,
        )
        _log(f"  Pushed to {pushed}/{len(instances)} instances")

        # Interval between registrations
        if infinite or i < count:
            wait = args.interval
            for _ in range(int(wait)):
                if _shutdown:
                    break
                time.sleep(1)

    _log(f"Done: {success} registered, {fail} failed")


if __name__ == "__main__":
    main()
