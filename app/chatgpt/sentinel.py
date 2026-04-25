"""
Sentinel / chat-requirements / POW token generation for chatgpt.com.

Implements:
- RequirementsToken (prefix gAAAAAC, 18-element config, fixed difficulty)
- ProofToken (prefix gAAAAAB, 13-element config, server-provided seed+difficulty)
- Single-step chat-requirements flow
- Two-step prepare + finalize flow
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as curl_requests
from loguru import logger


# ==================== POW Config ====================

class POWConfig:
    """Browser fingerprint config for PoW token generation."""

    def __init__(self, user_agent: str = ""):
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
        )
        self.sid = secrets.token_urlsafe(16)

    def _get_config_18(self) -> List[Any]:
        """18-element config for requirements token."""
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        nav_props = [
            "vendorSub", "productSub", "vendor", "maxTouchPoints",
            "scheduling", "userActivation", "doNotTrack", "geolocation",
            "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
            "webkitTemporaryStorage", "webkitPersistentStorage",
            "hardwareConcurrency", "cookieEnabled", "credentials",
            "mediaDevices", "permissions", "locks", "ink",
        ]
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

    def _get_config_13(self) -> List[Any]:
        """13-element config for proof token."""
        nav_props = [
            "vendorSub", "productSub", "vendor", "maxTouchPoints",
            "scheduling", "userActivation", "doNotTrack", "geolocation",
            "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
            "webkitTemporaryStorage", "webkitPersistentStorage",
            "hardwareConcurrency", "cookieEnabled", "credentials",
        ]
        nav_val = f"{random.choice(nav_props)}−undefined"
        return [
            "1920x1080", 4294705152, random.random(), self.user_agent,
            "en-US", "en-US,en", random.random(), nav_val,
            random.choice([4, 8, 12, 16]),
            self.sid, "", "", "",
        ]

    @staticmethod
    def _fnv1a_32(text: str) -> str:
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

    @staticmethod
    def _base64_encode(data: Any) -> str:
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return base64.b64encode(json_str.encode("utf-8")).decode("ascii")

    def requirements_token(self) -> str:
        """Generate requirements token (prefix gAAAAAC, no server interaction needed)."""
        config = self._get_config_18()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        data = self._base64_encode(config)
        return "gAAAAAC" + data

    def solve_proof(self, seed: str, difficulty: str, max_iter: int = 500000) -> str:
        """Solve proof-of-work challenge, return proof token (prefix gAAAAAB)."""
        config = self._get_config_13()
        start_time = time.time()
        for i in range(max_iter):
            config[2] = i
            config[9] = round((time.time() - start_time) * 1000)
            data = self._base64_encode(config)
            hash_hex = self._fnv1a_32(seed + data)
            if hash_hex[:len(difficulty)] <= difficulty:
                return "gAAAAAB" + data + "~S"
        # fallback: return error token
        return "gAAAAAB" + self._base64_encode(str(None))


# ==================== Chat Requirements Response ====================

class ChatRequirementsResult:
    def __init__(self, token: str = "", persona: str = "",
                 proofofwork_required: bool = False, proofofwork_seed: str = "",
                 proofofwork_difficulty: str = "", turnstile_required: bool = False,
                 turnstile_dx: str = "", proof_token: str = ""):
        self.token = token
        self.persona = persona
        self.proofofwork_required = proofofwork_required
        self.proofofwork_seed = proofofwork_seed
        self.proofofwork_difficulty = proofofwork_difficulty
        self.turnstile_required = turnstile_required
        self.turnstile_dx = turnstile_dx
        self.proof_token = proof_token


# ==================== Sentinel Client ====================

class SentinelClient:
    """Handles chat-requirements flow for chatgpt.com."""

    BASE_URL = "https://chatgpt.com"

    def __init__(self, access_token: str, device_id: str, session_id: str = "",
                 proxy: str = "", user_agent: str = "",
                 turnstile_solver_url: str = "", pow_max_iter: int = 500000):
        self.access_token = access_token
        self.device_id = device_id
        self.session_id = session_id
        self.proxy = proxy
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
        )
        self.turnstile_solver_url = turnstile_solver_url
        self.pow_max_iter = pow_max_iter
        self.pow_config = POWConfig(self.user_agent)

        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._impersonate = "chrome131"
        self._cf_bm = ""
        self._cfuvid = ""
        self._bootstrapped = False

    def _common_headers(self, path: str = "") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": self.user_agent,
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Arch": '"x86"',
            "Sec-Ch-Ua-Bitness": '"64"',
            "Sec-Ch-Ua-Full-Version": '"143.0.3650.96"',
            "Sec-Ch-Ua-Full-Version-List": '"Microsoft Edge";v="143.0.3650.96", "Chromium";v="143.0.7499.147", "Not A(Brand";v="24.0.0.0"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Ch-Ua-Platform-Version": '"19.0.0"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Oai-Device-Id": self.device_id,
            "Oai-Language": "zh-CN",
            "Oai-Client-Version": "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad",
            "Oai-Client-Build-Number": "5955942",
            "X-Openai-Target-Path": path,
            "X-Openai-Target-Route": path,
        }

    def bootstrap(self) -> bool:
        """GET chatgpt.com to acquire __cf_bm, _cfuvid, oai-did cookies."""
        try:
            resp = curl_requests.get(
                f"{self.BASE_URL}/",
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                proxies=self._proxies,
                impersonate=self._impersonate,
                timeout=30,
            )
            self._cf_bm = resp.cookies.get("__cf_bm", "")
            self._cfuvid = resp.cookies.get("_cfuvid", "")
            self._bootstrapped = True
            logger.debug(f"Bootstrap OK: cf_bm={bool(self._cf_bm)}, cfuvid={bool(self._cfuvid)}")
            return True
        except Exception as e:
            logger.warning(f"Bootstrap failed: {e}")
            return False

    def _ensure_bootstrap(self) -> None:
        if not self._bootstrapped:
            self.bootstrap()

    # ---------- Single-step chat-requirements ----------

    def chat_requirements_single(self) -> ChatRequirementsResult:
        """Single-step /backend-api/sentinel/chat-requirements."""
        self._ensure_bootstrap()
        path = "/backend-api/sentinel/chat-requirements"
        req_token = self.pow_config.requirements_token()

        resp = curl_requests.post(
            f"{self.BASE_URL}{path}",
            headers={
                **self._common_headers(path),
                "Content-Type": "application/json",
            },
            json={"p": req_token},
            proxies=self._proxies,
            impersonate=self._impersonate,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"chat-requirements failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        return ChatRequirementsResult(
            token=data.get("token", ""),
            persona=data.get("persona", ""),
            proofofwork_required=(data.get("proofofwork") or {}).get("required", False),
            proofofwork_seed=(data.get("proofofwork") or {}).get("seed", ""),
            proofofwork_difficulty=(data.get("proofofwork") or {}).get("difficulty", ""),
            turnstile_required=(data.get("turnstile") or {}).get("required", False),
            turnstile_dx=(data.get("turnstile") or {}).get("dx", ""),
        )

    # ---------- Two-step prepare + finalize ----------

    def chat_requirements_prepare(self) -> Dict[str, Any]:
        """POST /backend-api/sentinel/chat-requirements/prepare."""
        self._ensure_bootstrap()
        path = "/backend-api/sentinel/chat-requirements/prepare"
        req_token = self.pow_config.requirements_token()

        resp = curl_requests.post(
            f"{self.BASE_URL}{path}",
            headers={
                **self._common_headers(path),
                "Content-Type": "application/json",
            },
            json={"p": req_token},
            proxies=self._proxies,
            impersonate=self._impersonate,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"chat-requirements/prepare failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def chat_requirements_finalize(self, prepare_token: str,
                                   proofofwork: str = "",
                                   turnstile_resp: str = "") -> Tuple[str, str]:
        """POST /backend-api/sentinel/chat-requirements/finalize.
        Returns (token, persona).
        """
        path = "/backend-api/sentinel/chat-requirements/finalize"
        payload: Dict[str, Any] = {"prepare_token": prepare_token}
        if proofofwork:
            payload["proofofwork"] = proofofwork
        if turnstile_resp:
            payload["turnstile"] = turnstile_resp

        resp = curl_requests.post(
            f"{self.BASE_URL}{path}",
            headers={
                **self._common_headers(path),
                "Content-Type": "application/json",
            },
            json=payload,
            proxies=self._proxies,
            impersonate=self._impersonate,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"chat-requirements/finalize failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        return data.get("token", ""), data.get("persona", "")

    # ---------- Unified entry point (V2) ----------

    def get_chat_requirements(self) -> ChatRequirementsResult:
        """Get chat requirements token with 3-level fallback:
        1. Two-step (prepare + finalize) with Turnstile solver
        2. Turnstile VM solver (if configured)
        3. Single-step fallback (ignores Turnstile)
        """
        # Try two-step first
        try:
            prep = self.chat_requirements_prepare()
            prep_token = prep.get("prepare_token", "")
            persona = prep.get("persona", "")

            pow_required = (prep.get("proofofwork") or {}).get("required", False)
            ts_required = (prep.get("turnstile") or {}).get("required", False)
            pow_seed = (prep.get("proofofwork") or {}).get("seed", "")
            pow_diff = (prep.get("proofofwork") or {}).get("difficulty", "")

            # Solve POW if required
            proof = ""
            if pow_required and pow_seed:
                proof = self.pow_config.solve_proof(pow_seed, pow_diff, self.pow_max_iter)
                logger.debug(f"POW solved: required={pow_required}, len={len(proof)}")

            # Solve Turnstile if required
            ts_resp = ""
            if ts_required:
                ts_resp = self._solve_turnstile(prep.get("turnstile", {}).get("dx", ""))
                if not ts_resp:
                    logger.warning("Turnstile required but solver failed, falling back to single-step")
                    # Do NOT return here — let execution fall through to single-step
                    # fallback below so POW is also solved if needed.
                    raise RuntimeError("Turnstile solver failed, use single-step fallback")

            # Finalize
            token, final_persona = self.chat_requirements_finalize(prep_token, proof, ts_resp)
            if token:
                logger.info(f"Two-step chat-requirements OK, persona={final_persona or persona}")
                return ChatRequirementsResult(
                    token=token,
                    persona=final_persona or persona,
                    proofofwork_required=pow_required,
                    turnstile_required=ts_required,
                )
        except Exception as e:
            logger.warning(f"Two-step chat-requirements failed: {e}, falling back to single-step")

        # Fallback to single-step
        result = self.chat_requirements_single()

        # Solve POW if required
        if result.proofofwork_required and result.proofofwork_seed:
            proof = self.pow_config.solve_proof(
                result.proofofwork_seed, result.proofofwork_difficulty, self.pow_max_iter
            )
            result.proof_token = proof
            logger.debug(f"Single-step POW solved, len={len(proof)}")

        return result

    def _solve_turnstile(self, dx: str) -> str:
        """Solve Turnstile challenge via external solver or VM."""
        if self.turnstile_solver_url:
            try:
                resp = curl_requests.post(
                    f"{self.turnstile_solver_url.rstrip('/')}/turnstile",
                    json={"url": self.BASE_URL, "sitekey": dx},
                    timeout=60,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    task_id = data.get("taskId")
                    if task_id:
                        # Poll for result
                        for _ in range(30):
                            time.sleep(2)
                            r = curl_requests.get(
                                f"{self.turnstile_solver_url.rstrip('/')}/result",
                                params={"id": task_id},
                                timeout=20,
                            )
                            if r.status_code == 200:
                                rd = r.json()
                                token = (rd.get("solution") or {}).get("token") or rd.get("solution", {}).get("value", "")
                                if token and token != "CAPTCHA_FAIL":
                                    return token
                                if token == "CAPTCHA_FAIL":
                                    break
                logger.warning("Turnstile solver returned no valid token")
            except Exception as e:
                logger.warning(f"Turnstile solver error: {e}")
        return ""
