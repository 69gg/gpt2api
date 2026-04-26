#!/usr/bin/env python3
"""
reg_web.py — OpenAI 账号注册脚本（Web Token 版）

只做账号注册 + Platform OAuth 获取 web token，不做 Codex 部分。
注册成功后保存凭据到 web_token/ 目录，供 gpt2api 服务使用。

用法:
    python reg_web.py --cf-url https://xxx --cf-auth xxx
    python reg_web.py --cf-url https://xxx --proxy http://127.0.0.1:7890
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import secrets
import string
import sys
import time
import uuid
import hashlib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests

# ==================== 常量 ====================
BASE_DIR = Path(__file__).parent.parent.resolve()
WEB_TOKEN_DIR = BASE_DIR / "web_token"

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"

CLIENT_ID_CODEX = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI_CODEX = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.112 Safari/537.36"

_CHROME_PROFILES: List[Dict[str, Any]] = [
    {"major": 136, "impersonate": "chrome136", "build": 7103, "patch_range": (93, 112),
     "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not_A Brand";v="99"'},
]

_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9", "en-US,en;q=0.8", "en-GB,en;q=0.9,en-US;q=0.8",
    "zh-CN,zh;q=0.9,en;q=0.8", "ja-JP,ja;q=0.9,en;q=0.8",
]

_DEBUG = False


def _log(msg: str, level: str = "*") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def _debug(msg: str) -> None:
    if _DEBUG:
        _log(msg, "D")


def _mask(val: str, head: int = 24, tail: int = 8) -> str:
    if val and len(val) > head + tail:
        return f"{val[:head]}...{val[-tail:]}"
    return val


# ==================== 工具函数 ====================
def _random_state() -> str:
    return secrets.token_urlsafe(32)


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def _sha256_b64url_no_pad(s: str) -> str:
    raw = hashlib.sha256(s.encode("ascii")).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _gen_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    chars = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice("!@#$%^&*"),
    ]
    chars += [random.choice(alphabet) for _ in range(length - 4)]
    random.shuffle(chars)
    return "".join(chars)


def _gen_name() -> str:
    surnames = ("Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis")
    given = ("Alaric", "Bramwell", "Cedric", "Dorian", "Evander", "Fergus", "Gideon")
    return f"{random.choice(given)} {random.choice(surnames)}"


def _gen_birthdate() -> str:
    return f"{random.randint(1970, 1999)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _normalize_proxy_url(proxy_value: str) -> str:
    value = proxy_value.strip()
    if not value:
        raise ValueError("proxy cannot be empty")
    if "://" in value:
        return value
    if "@" in value:
        return f"http://{value}"
    parts = value.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        u, p = urllib.parse.quote(parts[2], safe=""), urllib.parse.quote(parts[3], safe="")
        return f"http://{u}:{p}@{parts[0]}:{parts[1]}"
    raise ValueError(f"unsupported proxy format: {value}")


def _generate_datadog_trace() -> Dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


# ==================== 浏览器指纹 ====================
def _random_chrome_profile() -> Dict[str, Any]:
    profile = random.choice(_CHROME_PROFILES)
    major = profile["major"]
    build = profile["build"]
    patch = random.randint(*profile["patch_range"])
    full_ver = f"{major}.0.{build}.{patch}"
    ua = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{full_ver} Safari/537.36"
    )
    return {
        "impersonate": profile["impersonate"],
        "major": major,
        "full_ver": full_ver,
        "ua": ua,
        "sec_ch_ua": profile["sec_ch_ua"],
    }


@dataclass
class BrowserFingerprint:
    user_agent: str
    platform: str
    impersonate: str = "chrome136"
    sec_ch_ua: str = ""
    chrome_full: str = ""
    accept_language: str = "en-US,en;q=0.9"

    @staticmethod
    def chrome_windows() -> "BrowserFingerprint":
        profile = _random_chrome_profile()
        return BrowserFingerprint(
            user_agent=profile["ua"],
            platform="Windows",
            impersonate=profile["impersonate"],
            sec_ch_ua=profile["sec_ch_ua"],
            chrome_full=profile["full_ver"],
            accept_language=random.choice(_ACCEPT_LANGUAGES),
        )


def _browser_identity_headers(user_agent: str, fp: Optional[BrowserFingerprint] = None) -> Dict[str, str]:
    major = re.search(r"Chrome/(\d+)", user_agent)
    major = major.group(1) if major else "120"
    sec_ch_ua = fp.sec_ch_ua if fp and fp.sec_ch_ua else f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"'
    chrome_full = fp.chrome_full if fp and fp.chrome_full else f"{major}.0.0.0"
    accept_lang = fp.accept_language if fp and fp.accept_language else "en-US,en;q=0.9"
    return {
        "user-agent": user_agent,
        "accept-language": accept_lang,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version": f'"{chrome_full}"',
        "sec-ch-ua-platform-version": f'"{random.randint(10, 15)}.0.0"',
    }


def _random_email_local() -> str:
    return f"oc{secrets.token_hex(5)}"


# ==================== 邮箱服务（CF Worker） ====================
class CFEmailProvider:
    def __init__(self, cf_url: str, cf_auth: str = "", cf_admin_auth: str = "",
                 cf_domain: str = "", proxies: Any = None):
        self.cf_url = cf_url.rstrip("/")
        self.cf_auth = cf_auth
        self.cf_admin_auth = cf_admin_auth
        # Support comma-separated domains with round-robin
        self._domains = [d.strip() for d in cf_domain.split(",") if d.strip()]
        self._domain_idx = 0
        self.proxies = proxies

    @property
    def cf_domain(self) -> str:
        if not self._domains:
            return ""
        return self._domains[self._domain_idx % len(self._domains)]

    def _next_domain(self) -> None:
        """Advance to next domain for round-robin."""
        if self._domains:
            self._domain_idx += 1

    def _headers(self, *, address_jwt: str = "") -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.cf_auth:
            h["x-custom-auth"] = self.cf_auth
        if self.cf_admin_auth:
            h["x-admin-auth"] = self.cf_admin_auth
        if address_jwt:
            h["Authorization"] = f"Bearer {address_jwt}"
        return h

    def create_email(self) -> Tuple[str, str]:
        use_admin_api = bool(self.cf_admin_auth)
        endpoint = f"{self.cf_url}/admin/new_address" if use_admin_api else f"{self.cf_url}/api/new_address"

        local = _random_email_local()
        data: Dict[str, Any] = {"name": local}
        if use_admin_api:
            data["enablePrefix"] = False
        current_domain = self.cf_domain
        if current_domain:
            data["domain"] = current_domain

        for attempt in range(1, 6):
            try:
                resp = requests.post(endpoint, json=data, headers=self._headers(),
                                     proxies=self.proxies, timeout=15, impersonate="chrome136")
            except Exception as e:
                if attempt >= 5:
                    raise RuntimeError(f"create_email failed after 5 attempts: {e}")
                time.sleep(2)
                continue

            if resp.status_code == 200:
                result = resp.json()
                email = str(result.get("address") or result.get("email") or "").strip()
                jwt_token = str(result.get("jwt") or result.get("token") or "").strip()
                if email and jwt_token:
                    self._next_domain()  # rotate to next domain
                    return email, jwt_token
                raise RuntimeError(f"create_email returned incomplete data: {result}")

            body = resp.text.strip().lower()
            if resp.status_code == 400 and current_domain and "invalid domain" in body:
                # Remove only the invalid domain, try next
                if current_domain in self._domains:
                    self._domains.remove(current_domain)
                    logger.warning(f"Removed invalid domain: {current_domain}, remaining: {self._domains}")
                current_domain = self.cf_domain  # get next domain
                data.pop("domain", None)
                if current_domain:
                    data["domain"] = current_domain
                time.sleep(1)
                continue
            if resp.status_code == 400 and ("already exists" in body or "unique" in body):
                data["name"] = _random_email_local()
                time.sleep(1)
                continue
            if resp.status_code == 429:
                time.sleep(10)
                continue
            raise RuntimeError(f"create_email failed: {resp.status_code} {resp.text[:200]}")

        raise RuntimeError("create_email failed after 5 attempts")

    def snapshot_mail_ids(self, jwt: str) -> set[str]:
        url = f"{self.cf_url}/api/mails?limit=20&offset=0"
        h = self._headers(address_jwt=jwt)
        try:
            resp = requests.get(url, headers=h, proxies=self.proxies,
                                timeout=30, impersonate="chrome136")
            if resp.status_code != 200:
                return set()
            emails = resp.json()
            results = emails.get("results") if isinstance(emails, dict) else emails
            if isinstance(results, list):
                return {str(e.get("id", "")) for e in results if e.get("id")}
        except Exception:
            pass
        return set()

    def wait_for_code(self, email: str, jwt: str, timeout: int = 90,
                      known_ids: Optional[set] = None) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                url = f"{self.cf_url}/api/mails?limit=20&offset=0"
                h = self._headers(address_jwt=jwt)
                resp = requests.get(url, headers=h, proxies=self.proxies,
                                    timeout=15, impersonate="chrome136")
                if resp.status_code == 200:
                    emails = resp.json()
                    results = emails.get("results") if isinstance(emails, dict) else emails
                    if isinstance(results, list):
                        for mail in results:
                            mid = str(mail.get("id", ""))
                            if not mid or (known_ids and mid in known_ids):
                                continue
                            # Fetch mail detail to get raw content
                            detail = self._get_mail_detail(jwt, mid)
                            code = self._extract_otp_from_mail(mail, detail)
                            if code:
                                return code
            except Exception:
                pass
            time.sleep(3)
        return ""

    def _get_mail_detail(self, jwt: str, mail_id: str) -> Dict[str, Any]:
        url = f"{self.cf_url}/api/mail/{mail_id}"
        h = self._headers(address_jwt=jwt)
        try:
            resp = requests.get(url, headers=h, proxies=self.proxies,
                                timeout=15, impersonate="chrome136")
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    @staticmethod
    def _extract_otp_from_mail(summary: Dict[str, Any], detail: Dict[str, Any]) -> str:
        # Only extract from OpenAI emails to avoid wrong codes from other mail
        source = str(summary.get("source") or summary.get("from") or detail.get("source") or detail.get("from") or "")
        subject = str(summary.get("subject") or detail.get("subject") or "")
        text = str(summary.get("text") or detail.get("text") or "")
        html = str(summary.get("html") or detail.get("html") or "")
        raw = str(detail.get("raw") or summary.get("raw") or "")

        searchable = f"{subject} {text} {html} {source}".strip()
        # Check if this is an OpenAI email
        identity_markers = ("openai", "chatgpt", "codex")
        normalized = searchable.lower()
        is_oai = any(m in normalized for m in identity_markers)
        if not is_oai and raw:
            is_oai = any(m in raw.lower() for m in identity_markers)

        # Try subject first
        code_match = re.search(r'(?<!\d)(\d{6})(?!\d)', subject)
        if code_match and is_oai:
            return code_match.group(1)

        # Parse raw email content
        if raw:
            from email.parser import BytesParser
            from email import policy
            try:
                msg = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8", "replace"))
                parsed_subject = str(msg.get("subject") or "")
                # Try parsed subject
                code_match = re.search(r'(?<!\d)(\d{6})(?!\d)', parsed_subject)
                if code_match and is_oai:
                    return code_match.group(1)
                parts = msg.walk() if msg.is_multipart() else [msg]
                for part in parts:
                    if part.is_multipart() or (part.get_content_disposition() or "").lower() == "attachment":
                        continue
                    try:
                        content = part.get_content()
                        if isinstance(content, bytes):
                            charset = part.get_content_charset() or "utf-8"
                            content = content.decode(charset, "replace")
                        if not content:
                            continue
                        content_str = str(content)
                        # Remove CSS hex colors (#123456) to avoid false matches
                        cleaned = re.sub(r'#[0-9a-fA-F]{6}\b', '', content_str)
                        code_match = re.search(r'(?<!\d)(\d{6})(?!\d)', cleaned)
                        if code_match:
                            return code_match.group(1)
                    except Exception:
                        continue
            except Exception:
                pass
        # Fallback: try text/html fields from detail (also strip hex colors)
        if is_oai:
            for field in ("text", "html"):
                content = str(detail.get(field, ""))
                if content:
                    cleaned = re.sub(r'#[0-9a-fA-F]{6}\b', '', content)
                    code_match = re.search(r'(?<!\d)(\d{6})(?!\d)', cleaned)
                    if code_match:
                        return code_match.group(1)
        return ""


# ==================== Sentinel Token 生成器 ====================
class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id=None, user_agent=None):
        self.device_id = device_id or secrets.token_urlsafe(16)
        self.user_agent = user_agent or UA
        self.requirements_seed = str(random.random())
        self.sid = secrets.token_urlsafe(16)

    @staticmethod
    def _fnv1a_32(text):
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        nav_props = ["vendorSub", "productSub", "vendor", "maxTouchPoints",
                     "scheduling", "userActivation", "doNotTrack", "geolocation",
                     "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
                     "webkitTemporaryStorage", "webkitPersistentStorage",
                     "hardwareConcurrency", "cookieEnabled", "credentials",
                     "mediaDevices", "permissions", "locks", "ink"]
        nav_val = f"{random.choice(nav_props)}−undefined"
        doc_key = random.choice(["location", "implementation", "URL", "documentURI", "compatMode"])
        win_key = random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"])
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080", date_str, 4294705152, random.random(), self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js", None, None,
            "en-US", "en-US,en", random.random(), nav_val, doc_key, win_key,
            perf_now, self.sid, "", random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _base64_encode(data):
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return base64.b64encode(json_str.encode("utf-8")).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = self._fnv1a_32(seed + data)
        if hash_hex[:len(difficulty)] <= difficulty:
            return data + "~S"
        return None

    def generate_token(self, seed=None, difficulty=None):
        if seed is None:
            seed = self.requirements_seed
            difficulty = difficulty or "0"
        start_time = time.time()
        config = self._get_config()
        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start_time, seed, difficulty, config, i)
            if result:
                return "gAAAAAB" + result
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self):
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


# ==================== Sentinel Bundle ====================
@dataclass
class SentinelBundle:
    did: str
    signup_sentinel: str
    register_sentinel: str
    password_verify_sentinel: str
    oai_sc: str
    user_agent: str


def _fetch_sentinel_challenge(session, device_id, flow="authorize_continue", user_agent=None):
    gen = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    body = {"p": gen.generate_requirements_token(), "id": device_id, "flow": flow}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent or UA,
    }
    try:
        resp = session.post("https://sentinel.openai.com/backend-api/sentinel/req",
                            data=json.dumps(body), headers=headers, timeout=20,
                            impersonate="chrome136")
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _build_sentinel_token(session, device_id, flow="authorize_continue", user_agent=None):
    challenge = _fetch_sentinel_challenge(session, device_id, flow=flow, user_agent=user_agent)
    if not challenge or not challenge.get("token"):
        return None
    gen = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    pow_data = challenge.get("proofofwork") or {}
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = gen.generate_token(seed=pow_data["seed"], difficulty=pow_data.get("difficulty", "0"))
    else:
        p_value = gen.generate_requirements_token()
    dummy_t = bytes(random.getrandbits(8) for _ in range(3500))
    t_value = "Th" + base64.b64encode(dummy_t).decode("ascii")[2:]
    return json.dumps({"p": p_value, "t": t_value, "c": challenge["token"],
                       "id": device_id, "flow": flow}, separators=(",", ":"))


def _fetch_sentinel_bundle(session, did: str, user_agent: str = None) -> SentinelBundle:
    ua = user_agent or UA
    signup = _build_sentinel_token(session, did, flow="authorize_continue", user_agent=ua)
    register = _build_sentinel_token(session, did, flow="username_password_create", user_agent=ua)
    pwd_verify = _build_sentinel_token(session, did, flow="password_verify", user_agent=ua)
    if not signup or not register or not pwd_verify:
        raise RuntimeError("Sentinel token generation failed")
    return SentinelBundle(
        did=did, signup_sentinel=signup, register_sentinel=register,
        password_verify_sentinel=pwd_verify, oai_sc=secrets.token_urlsafe(32),
        user_agent=ua,
    )


# ==================== Flow Context ====================
@dataclass
class FlowContext:
    fingerprint: BrowserFingerprint = field(default_factory=BrowserFingerprint.chrome_windows)
    impersonate: str = ""
    user_agent: str = ""
    did: str = ""
    auth_url: str = ""
    auth_state: str = ""
    code_verifier: str = ""
    redirect_uri: str = "https://platform.openai.com/auth/callback"
    client_id: str = "app_2SKx67EdpoN0G6j64rFvigXD"
    email: str = ""
    email_jwt: str = ""
    password: str = ""
    known_mail_ids: set = field(default_factory=set)
    signup_sentinel: str = ""
    register_sentinel: str = ""
    password_verify_sentinel: str = ""
    oai_sc: str = ""
    so_token: str = ""
    token_json: str = ""
    callback_url: str = ""


# ==================== Auth API Headers ====================
def _auth_headers(user_agent: str, referer: str, content_type_json: bool = False,
                  include_origin: bool = False, sentinel_token: str = "",
                  so_token: str = "", fp: Optional[BrowserFingerprint] = None,
                  device_id: str = "") -> Dict[str, str]:
    headers = {
        **_browser_identity_headers(user_agent, fp=fp),
        "accept": "application/json",
        "referer": referer,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    headers.update(_generate_datadog_trace())
    if content_type_json:
        headers["content-type"] = "application/json"
    if include_origin:
        headers["origin"] = "https://auth.openai.com"
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    if so_token:
        headers["openai-sentinel-so-token"] = so_token
    if device_id:
        headers["oai-device-id"] = device_id
    return headers


# ==================== OAuth Code Extraction ====================
def _extract_code_from_url(url_str: str) -> Optional[str]:
    if not url_str:
        return None
    parsed = urllib.parse.urlparse(url_str)
    params = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    for d in (params, fragment):
        codes = d.get("code", [])
        if codes and codes[0]:
            return codes[0].strip()
    return None


def _oauth_follow_for_code(session, start_url: str, *, referer: str = "",
                           user_agent: str = "", impersonate: str = "chrome136",
                           max_hops: int = 16) -> Tuple[Optional[str], str]:
    if "code=" in start_url:
        code = _extract_code_from_url(start_url)
        if code:
            return code, start_url
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": user_agent or UA,
    }
    if referer:
        headers["Referer"] = referer
    current_url = start_url
    last_url = start_url
    for hop in range(max_hops):
        try:
            resp = session.get(current_url, headers=headers, allow_redirects=False,
                               timeout=30, impersonate=impersonate)
            last_url = str(resp.url)
            _debug(f"[OAuth follow][{hop+1}] {resp.status_code} {last_url[:80]}")
        except Exception as exc:
            maybe = re.search(r'(https?://localhost[^\s\'"]+)', str(exc))
            if maybe:
                code = _extract_code_from_url(maybe.group(1))
                if code:
                    return code, maybe.group(1)
            return None, last_url
        code = _extract_code_from_url(last_url)
        if code:
            return code, last_url
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if not location:
                return None, last_url
            if location.startswith("/"):
                p = urllib.parse.urlparse(current_url)
                location = f"{p.scheme}://{p.netloc}{location}"
            code = _extract_code_from_url(location)
            if code:
                return code, location
            current_url = location
            headers["Referer"] = last_url
        else:
            return None, last_url
    return None, last_url


# ==================== Token Exchange ====================
def _post_form(url: str, data: Dict[str, str], proxies: Any = None,
               impersonate: str = "chrome136") -> Dict[str, Any]:
    resp = requests.post(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": UA,
    }, proxies=proxies, impersonate=impersonate, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange failed: {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _build_token_json(*, access_token: str, refresh_token: str,
                      id_token: str = "", client_id: str = "",
                      expires_in: int = 0, email: str = "",
                      password: str = "", proxy: str = "") -> Dict[str, Any]:
    claims = _jwt_claims_no_verify(id_token)
    access_claims = _jwt_claims_no_verify(access_token)

    token_email = str(claims.get("email") or "").strip()
    if not token_email:
        profile = access_claims.get("https://api.openai.com/profile") or {}
        token_email = str(access_claims.get("email") or profile.get("email") or "").strip()

    account_id = ""
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()
    if not account_id:
        auth_claims = access_claims.get("https://api.openai.com/auth") or {}
        account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()

    now = int(time.time())
    now_rfc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    expired_rfc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 3600)))

    return {
        "email": token_email or email,
        "password": password,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "client_id": client_id,
        "account_id": account_id,
        "plan_type": "free",
        "expired": expired_rfc,
        "last_refresh": now_rfc,
        "registered_at": now_rfc,
        "status": "active",
        "fail_count": 0,
        "last_fail_at": None,
        "last_fail_reason": None,
        "cooldown_until": None,
        "use_count": 0,
        "last_used_at": None,
        "daily_quota_remaining": None,
        "daily_quota_total": None,
        "image_quota_remaining": None,
        "image_quota_total": None,
        "user_agent": "",
        "impersonate": "",
        "proxy": proxy,
    }


def _exchange_code_for_token(session, code: str, state: str, code_verifier: str,
                             client_id: str, redirect_uri: str,
                             proxies: Any = None, email: str = "",
                             password: str = "", proxy: str = "") -> Dict[str, Any]:
    token_resp = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }, proxies=proxies)

    return _build_token_json(
        access_token=str(token_resp.get("access_token") or "").strip(),
        refresh_token=str(token_resp.get("refresh_token") or "").strip(),
        id_token=str(token_resp.get("id_token") or "").strip(),
        client_id=client_id,
        expires_in=_to_int(token_resp.get("expires_in")),
        email=email,
        password=password,
        proxy=proxy,
    )


# ==================== Registration Flow ====================
def register_account(session, context: FlowContext, email_provider: CFEmailProvider,
                     proxies: Any = None, proxy: str = "") -> Dict[str, Any]:
    """Run the full registration flow and return token dict."""

    # Step 1: Prepare OAuth runtime
    _log("[01] Preparing OAuth runtime...")
    fp = context.fingerprint
    context.impersonate = fp.impersonate
    context.user_agent = fp.user_agent

    # Pre-generate device_id and set cookie (matching reg_best.py flow)
    if not context.did:
        context.did = str(uuid.uuid4())
    session.cookies.set("oai-did", context.did, domain=".openai.com")

    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    params = {
        "client_id": context.client_id,
        "response_type": "code",
        "redirect_uri": context.redirect_uri,
        "scope": DEFAULT_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
    }
    context.auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    context.auth_state = state
    context.code_verifier = code_verifier

    resp = session.get(context.auth_url, timeout=30, impersonate=context.impersonate)
    _debug(f"OAuth page status: {resp.status_code}")

    # Update did from server if provided
    server_did = resp.cookies.get("oai-did") or ""
    if server_did:
        context.did = server_did
    _log(f"  did: {_mask(context.did, 12, 8)}")

    # Step 2: Fetch sentinel bundle
    _log("[02] Fetching sentinel bundle...")
    bundle = _fetch_sentinel_bundle(session, context.did, context.user_agent)
    context.signup_sentinel = bundle.signup_sentinel
    context.register_sentinel = bundle.register_sentinel
    context.password_verify_sentinel = bundle.password_verify_sentinel
    context.oai_sc = bundle.oai_sc
    context.user_agent = bundle.user_agent
    session.headers.update({"user-agent": context.user_agent})
    session.cookies.set("oai-sc", context.oai_sc, domain=".openai.com", path="/", secure=True)

    # Step 3: Submit email
    _log("[03] Creating email & submitting...")
    email, jwt = email_provider.create_email()
    context.email = email
    context.email_jwt = jwt
    _log(f"  email: {email}")

    resp = session.post(
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers=_auth_headers(
            user_agent=context.user_agent,
            referer="https://auth.openai.com/create-account",
            content_type_json=True, include_origin=True,
            sentinel_token=context.signup_sentinel,
            fp=context.fingerprint, device_id=context.did,
        ),
        json={"username": {"value": email, "kind": "email"}, "screen_hint": "signup"},
        timeout=15,
    )
    _debug(f"Submit email: {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"Submit email failed: {resp.text[:200]}")

    # Step 4: Submit password
    _log("[04] Setting password...")
    context.password = _gen_password()
    _log(f"  password: {context.password}")

    resp = session.post(
        "https://auth.openai.com/api/accounts/user/register",
        headers=_auth_headers(
            user_agent=context.user_agent,
            referer="https://auth.openai.com/create-account/password",
            content_type_json=True, include_origin=True,
            sentinel_token=context.register_sentinel,
            fp=context.fingerprint, device_id=context.did,
        ),
        json={"password": context.password, "username": context.email},
        timeout=15,
    )
    _debug(f"Set password: {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"Set password failed: {resp.text[:200]}")

    # Step 5: Send + validate OTP
    _log("[05] Sending OTP...")
    context.known_mail_ids = email_provider.snapshot_mail_ids(context.email_jwt)

    resp = session.get(
        "https://auth.openai.com/api/accounts/email-otp/send",
        headers=_auth_headers(
            user_agent=context.user_agent,
            referer="https://auth.openai.com/email-verification",
            content_type_json=True,
            sentinel_token=context.signup_sentinel,
        ),
        timeout=15,
    )
    _debug(f"Send OTP: {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"Send OTP failed: {resp.text[:200]}")

    _log("[06] Waiting for OTP code...")
    code = email_provider.wait_for_code(
        context.email, context.email_jwt, timeout=90,
        known_ids=context.known_mail_ids,
    )
    if not code:
        raise RuntimeError("OTP timeout")

    _log(f"  OTP code: {code}")
    resp = session.post(
        "https://auth.openai.com/api/accounts/email-otp/validate",
        headers=_auth_headers(
            user_agent=context.user_agent,
            referer="https://auth.openai.com/email-verification",
            content_type_json=True, include_origin=True,
            sentinel_token=context.signup_sentinel,
        ),
        json={"code": code},
        timeout=15,
    )
    _debug(f"Validate OTP: {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"Validate OTP failed: {resp.text[:200]}")
    _log("  OTP validated!")

    # Step 6: Create account profile
    _log("[07] Creating account profile...")
    name = _gen_name()
    birth = _gen_birthdate()

    so_challenge = _fetch_sentinel_challenge(session, context.did, flow="oauth_create_account",
                                             user_agent=context.user_agent)
    so_sentinel = _build_sentinel_token(session, context.did, flow="oauth_create_account",
                                        user_agent=context.user_agent)
    so_token_str = ""
    if so_challenge and so_challenge.get("token"):
        dummy = bytes(random.getrandbits(8) for _ in range(550))
        fake_so = "Th" + base64.b64encode(dummy).decode("ascii")[2:]
        so_token_str = json.dumps({
            "so": fake_so, "c": so_challenge["token"],
            "id": context.did, "flow": "oauth_create_account",
        }, separators=(",", ":"))

    headers = _auth_headers(
        user_agent=context.user_agent,
        referer="https://auth.openai.com/about-you",
        content_type_json=True, include_origin=True,
        so_token=so_token_str or context.so_token,
        sentinel_token=so_sentinel or "",
        fp=context.fingerprint, device_id=context.did,
    )
    payload = {"name": name, "birthdate": birth}

    resp = session.post("https://auth.openai.com/api/accounts/create_account",
                        headers=headers, json=payload, timeout=15)
    _debug(f"Create account: {resp.status_code}")

    if resp.status_code == 400 and "invalid_auth_step" in (resp.text or ""):
        _log("  Hit invalid_auth_step, warming up...")
        for warm_url in ("https://auth.openai.com/about-you",
                         "https://auth.openai.com/api/accounts/authorize/callback"):
            try:
                session.get(warm_url, headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Upgrade-Insecure-Requests": "1",
                    "Referer": "https://auth.openai.com/email-verification",
                    "User-Agent": context.user_agent,
                }, allow_redirects=True, timeout=30)
            except Exception:
                pass
        headers.update(_generate_datadog_trace())
        resp = session.post("https://auth.openai.com/api/accounts/create_account",
                            headers=headers, json=payload, timeout=15)
        _debug(f"Create account (retry): {resp.status_code}")

    if resp.status_code != 200:
        raise RuntimeError(f"Create account failed: {resp.text[:200]}")

    # Parse create_account response for continue_url
    try:
        create_json = resp.json()
    except Exception:
        create_json = {}
    continue_url = str((create_json or {}).get("continue_url") or "").strip()
    page_type = str((((create_json or {}).get("page") or {}).get("type")) or "").strip()
    _debug(f"Create account page_type={page_type}, continue_url={continue_url[:80] if continue_url else '-'}")

    # Step 7: Get OAuth token via Platform OAuth (continue_url)
    _log("[08] Getting Platform OAuth token...")
    token_data = None
    if continue_url:
        token_data = _follow_continue_url_for_token(
            session, context, continue_url, proxies=proxies, proxy=proxy,
        )
    if not token_data:
        raise RuntimeError("Failed to obtain Platform OAuth token")

    _log("  Platform token obtained!")
    # Persist browser fingerprint so TokenManager can recreate the exact client later
    if token_data:
        token_data["user_agent"] = context.fingerprint.user_agent
        token_data["impersonate"] = getattr(context.fingerprint, "impersonate", "")
        token_data["device_id"] = context.did
        token_data["session_id"] = str(uuid.uuid4())
        token_data["proxy"] = proxy
    return token_data




def _follow_continue_url_for_token(
    session, context, continue_url: str, *,
    proxies: Any = None, proxy: str = "", max_redirects: int = 15,
) -> Optional[Dict[str, Any]]:
    """Follow the continue_url redirect chain to extract OAuth code and exchange for token.
    Handles OIDC hybrid flow where callback URL contains #code=... fragment.
    """
    current_url = urllib.parse.urljoin("https://auth.openai.com", continue_url.strip())

    # Check if continue_url already contains a code
    code = _extract_code_from_url(current_url)
    if code:
        try:
            return _exchange_code_for_token(
                session, code, context.auth_state, context.code_verifier,
                context.client_id, context.redirect_uri,
                proxies=proxies, email=context.email, password=context.password,
                proxy=proxy,
            )
        except Exception:
            pass

    for _ in range(max_redirects):
        try:
            _debug(f"[follow-continue] GET {current_url[:80]}...")
            resp = session.get(current_url, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": context.user_agent,
                "Referer": "https://auth.openai.com/about-you",
            }, allow_redirects=False, timeout=30, impersonate=context.impersonate)
            _debug(f"[follow-continue] status={resp.status_code} loc={resp.headers.get('Location', '')[:80]}")
        except Exception as exc:
            # curl_cffi throws on localhost redirect — extract code from error
            maybe = re.search(r'(https?://localhost[^\s\'"]+)', str(exc))
            if maybe:
                code = _extract_code_from_url(maybe.group(1))
                if code:
                    try:
                        return _exchange_code_for_token(
                            session, code, context.auth_state, context.code_verifier,
                            context.client_id, context.redirect_uri,
                            proxies=proxies, email=context.email, password=context.password,
                        )
                    except Exception:
                        pass
            break

        status = resp.status_code
        location = resp.headers.get("Location") or ""

        if status not in (301, 302, 303, 307, 308):
            break
        if not location:
            break

        next_url = urllib.parse.urljoin(current_url, location)

        # Check for code in Location header
        code = _extract_code_from_url(next_url)
        if code:
            try:
                return _exchange_code_for_token(
                    session, code, context.auth_state, context.code_verifier,
                    context.client_id, context.redirect_uri,
                    proxies=proxies, email=context.email, password=context.password,
                )
            except Exception:
                pass

        # Handle login_challenge → OAuth authorize → callback#code=... (OIDC fragment)
        if "login_challenge" in next_url or "/api/accounts/login" in next_url:
            _debug(f"[continue] Detected login_challenge, extracting OIDC fragment")
            try:
                # Re-request current_url to get the OAuth authorize redirect
                lc_resp = session.get(current_url, headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": context.user_agent,
                }, allow_redirects=False, timeout=15, impersonate=context.impersonate)
                lc_loc = lc_resp.headers.get("Location") or ""
                if lc_loc:
                    oauth_req_url = urllib.parse.urljoin(current_url, lc_loc)
                    oa_resp = session.get(oauth_req_url, headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "User-Agent": context.user_agent,
                    }, allow_redirects=False, timeout=15, impersonate=context.impersonate)
                    oa_loc = oa_resp.headers.get("Location") or ""
                    if oa_loc:
                        if oa_loc.startswith("#"):
                            full_callback = current_url + oa_loc
                        else:
                            full_callback = urllib.parse.urljoin(oauth_req_url, oa_loc)
                        code = _extract_code_from_url(full_callback)
                        if code:
                            try:
                                return _exchange_code_for_token(
                                    session, code, context.auth_state, context.code_verifier,
                                    context.client_id, context.redirect_uri,
                                    proxies=proxies, email=context.email, password=context.password,
                                )
                            except Exception:
                                pass
            except Exception:
                pass

        current_url = next_url

    return None


def _extract_workspace_id(session) -> str:
    """Extract workspace_id from oai-client-auth-session cookie."""
    import gzip as _gzip
    import zlib as _zlib

    for domain in (".auth.openai.com", "auth.openai.com"):
        try:
            cookie_val = session.cookies.get("oai-client-auth-session", domain=domain)
            if not cookie_val:
                continue
        except Exception:
            continue

        # Decode JWT-like structure to find workspace_id
        try:
            parts = cookie_val.split(".")
            if len(parts) >= 2:
                payload = parts[1]
                # Add padding
                payload += "=" * (4 - len(payload) % 4)
                decoded = base64.b64decode(payload)
                data = json.loads(decoded)
                wid = str(data.get("workspace_id") or "")
                if wid:
                    return wid
        except Exception:
            pass

        # Try decompressing gzip/zlib content
        try:
            raw = base64.b64decode(cookie_val + "=" * (-len(cookie_val) % 4))
            for decompress in (lambda d: _gzip.decompress(d), lambda d: _zlib.decompress(d)):
                try:
                    text = decompress(raw).decode("utf-8", "replace")
                    m = re.search(r'"workspace_id"\s*:\s*"([^"]+)"', text)
                    if m:
                        return m.group(1)
                except Exception:
                    continue
        except Exception:
            pass
    return ""


def _select_workspace_and_exchange(
    session, context, workspace_id: str, *,
    proxies: Any = None,
) -> Optional[Dict[str, Any]]:
    """Select workspace and follow continue_url to get token."""
    resp = session.post(
        "https://auth.openai.com/api/accounts/workspace/select",
        headers=_auth_headers(
            user_agent=context.user_agent,
            referer="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            content_type_json=True, include_origin=True,
            fp=context.fingerprint, device_id=context.did,
        ),
        json={"workspace_id": workspace_id},
        timeout=15,
    )
    if resp.status_code != 200:
        _debug(f"workspace/select failed: {resp.status_code}")
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    continue_url = str((data or {}).get("continue_url") or "").strip()
    if not continue_url:
        return None

    return _follow_continue_url_for_token(
        session, context, continue_url, proxies=proxies, proxy=proxy,
    )



def _build_oauth_authorize_url(*, client_id: str, redirect_uri: str,
                               prompt: str = "login",
                               simplified_flow: bool = False) -> Tuple[str, str, str]:
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": DEFAULT_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": prompt,
        "id_token_add_organizations": "true",
    }
    if simplified_flow:
        params["codex_cli_simplified_flow"] = "true"
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state, code_verifier


def save_token(token_data: Dict[str, Any]) -> Path:
    """Save token to web_token/{email}.json"""
    WEB_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    email = token_data.get("email", "unknown")
    safe_name = email.replace("@", "_at_").replace(".", "_")
    path = WEB_TOKEN_DIR / f"{safe_name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(token_data, f, ensure_ascii=False, indent=2)
    _log(f"Token saved to {path}")
    return path


def main() -> None:
    global _DEBUG

    parser = argparse.ArgumentParser(description="OpenAI account registration (Web Token)")
    parser.add_argument("--cf-url", required=True, help="Cloudflare email backend URL")
    parser.add_argument("--cf-auth", default="", help="CF site auth token")
    parser.add_argument("--cf-admin-auth", default="", help="CF admin auth token")
    parser.add_argument("--cf-domain", default="", help="Email domain override")
    parser.add_argument("--proxy", default=None, help="Proxy URL")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    _DEBUG = args.debug

    fp = BrowserFingerprint.chrome_windows()
    _log(f"Fingerprint: {fp.impersonate}, Chrome/{fp.chrome_full}")

    proxies = None
    if args.proxy:
        proxy_url = _normalize_proxy_url(args.proxy)
        proxies = {"http": proxy_url, "https": proxy_url}
        _log(f"Using proxy: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")

    session = requests.Session(impersonate=fp.impersonate, proxies=proxies)
    session.headers.update(_browser_identity_headers(fp.user_agent, fp=fp))

    email_provider = CFEmailProvider(
        cf_url=args.cf_url, cf_auth=args.cf_auth,
        cf_admin_auth=args.cf_admin_auth, cf_domain=args.cf_domain,
        proxies=proxies,
    )

    context = FlowContext(
        fingerprint=fp,
        redirect_uri="https://platform.openai.com/auth/callback",
        client_id="app_2SKx67EdpoN0G6j64rFvigXD",
    )
    context.did = str(uuid.uuid4())
    session.cookies.set("oai-did", context.did, domain="chatgpt.com")

    try:
        token_data = register_account(session, context, email_provider, proxies=proxies)
        path = save_token(token_data)
        _log(f"Registration complete! Email: {context.email}")
        _log(f"Token file: {path}")
    except Exception as e:
        _log(f"Registration failed: {e}", "!")
        sys.exit(1)


if __name__ == "__main__":
    main()
