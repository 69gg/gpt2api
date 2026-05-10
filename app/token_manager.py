"""
Token pool management — load, refresh, invalidate, delete, replenish.

Token states:
- active: ready to use
- cooling: quota exhausted, waiting for reset
- expired: access_token expired, needs refresh
- dead: account banned or permanently failed
- disabled: manually disabled
- registering: account being registered
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.config import get_config


class TokenStatus(str, Enum):
    ACTIVE = "active"
    COOLING = "cooling"
    STALE = "stale"
    EXPIRED = "expired"
    DEAD = "dead"
    DISABLED = "disabled"
    REGISTERING = "registering"


class FailReason(str, Enum):
    BANNED = "banned"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TOKEN_EXPIRED = "token_expired"
    RATE_LIMITED = "rate_limited"
    CF_CHALLENGE = "cf_challenge"
    UNKNOWN = "unknown"


@dataclass
class TokenInfo:
    """Represents a single account/token entry."""
    email: str = ""
    password: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    client_id: str = ""
    account_id: str = ""
    device_id: str = ""
    session_id: str = ""
    status: TokenStatus = TokenStatus.ACTIVE
    plan_type: str = "free"
    expired: str = ""
    last_refresh: str = ""
    registered_at: str = ""
    fail_count: int = 0
    last_fail_at: Optional[float] = None
    last_fail_reason: Optional[str] = None
    cooldown_until: Optional[float] = None
    use_count: int = 0
    last_used_at: Optional[float] = None
    daily_quota_remaining: Optional[int] = None
    daily_quota_total: Optional[int] = None
    image_quota_remaining: Optional[int] = None
    image_quota_total: Optional[int] = None
    user_agent: str = ""
    impersonate: str = ""
    proxy: str = ""  # per-token proxy (http://..., socks5://..., socks5h://...)
    _path: Optional[str] = None  # file path for persistence

    @staticmethod
    def from_dict(data: Dict[str, Any], path: str = "") -> "TokenInfo":
        status_str = data.get("status", "active")
        try:
            status = TokenStatus(status_str)
        except ValueError:
            status = TokenStatus.ACTIVE

        return TokenInfo(
            email=data.get("email", ""),
            password=data.get("password", ""),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            id_token=data.get("id_token", ""),
            client_id=data.get("client_id", ""),
            account_id=data.get("account_id", ""),
            device_id=data.get("device_id") or str(uuid.uuid4()),
            session_id=data.get("session_id") or str(uuid.uuid4()),
            status=status,
            plan_type=data.get("plan_type", "free"),
            expired=data.get("expired", ""),
            last_refresh=data.get("last_refresh", ""),
            registered_at=data.get("registered_at", ""),
            fail_count=data.get("fail_count", 0),
            last_fail_at=data.get("last_fail_at"),
            last_fail_reason=data.get("last_fail_reason"),
            cooldown_until=data.get("cooldown_until"),
            use_count=data.get("use_count", 0),
            last_used_at=data.get("last_used_at"),
            daily_quota_remaining=data.get("daily_quota_remaining"),
            daily_quota_total=data.get("daily_quota_total"),
            image_quota_remaining=data.get("image_quota_remaining"),
            image_quota_total=data.get("image_quota_total"),
            user_agent=data.get("user_agent", ""),
            impersonate=data.get("impersonate", ""),
            proxy=data.get("proxy", ""),
            _path=path,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "password": self.password,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "client_id": self.client_id,
            "account_id": self.account_id,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "plan_type": self.plan_type,
            "expired": self.expired,
            "last_refresh": self.last_refresh,
            "registered_at": self.registered_at,
            "fail_count": self.fail_count,
            "last_fail_at": self.last_fail_at,
            "last_fail_reason": self.last_fail_reason,
            "cooldown_until": self.cooldown_until,
            "use_count": self.use_count,
            "last_used_at": self.last_used_at,
            "daily_quota_remaining": self.daily_quota_remaining,
            "daily_quota_total": self.daily_quota_total,
            "image_quota_remaining": self.image_quota_remaining,
            "image_quota_total": self.image_quota_total,
            "user_agent": self.user_agent,
            "impersonate": self.impersonate,
            "proxy": self.proxy,
        }

    def get_proxy(self, global_proxy: str = "") -> str:
        """Return effective proxy: token-level if set, otherwise global."""
        return self.proxy if self.proxy else global_proxy

    @property
    def is_available(self) -> bool:
        if self.status != TokenStatus.ACTIVE:
            return False
        if not self.access_token:
            return False
        if self.cooldown_until and time.time() < self.cooldown_until:
            return False
        return True

    @property
    def is_expired(self) -> bool:
        if not self.expired:
            return False
        try:
            from datetime import datetime
            exp = datetime.fromisoformat(self.expired.replace("Z", "+00:00"))
            return exp.timestamp() < time.time()
        except Exception:
            return False

    def record_success(self) -> None:
        self.use_count += 1
        self.last_used_at = time.time()
        self.fail_count = 0  # reset fail count on success
        self.last_fail_reason = None

    def record_failure(self, reason: FailReason = FailReason.UNKNOWN) -> None:
        self.fail_count += 1
        self.last_fail_at = time.time()
        self.last_fail_reason = reason.value

        threshold = get_config("token.fail_threshold", 5)

        if reason == FailReason.BANNED:
            self.status = TokenStatus.DEAD
            logger.warning(f"Token {self.email} marked DEAD (banned)")
        elif reason == FailReason.QUOTA_EXHAUSTED:
            cooling_hours = get_config("token.cooling_reset_hours", 24)
            self.cooldown_until = time.time() + cooling_hours * 3600
            self.status = TokenStatus.COOLING
            logger.info(f"Token {self.email} marked COOLING (quota exhausted, {cooling_hours}h)")
        elif reason == FailReason.TOKEN_EXPIRED:
            self.status = TokenStatus.STALE
            logger.info(f"Token {self.email} marked STALE (refresh failed, count={self.fail_count})")
            if self.fail_count >= 3:
                self.status = TokenStatus.EXPIRED
                logger.info(f"Token {self.email} marked EXPIRED (refresh failed {self.fail_count} times)")
        elif self.fail_count >= threshold:
            self.status = TokenStatus.DEAD
            logger.warning(f"Token {self.email} marked DEAD (fail_count={self.fail_count})")

    def save(self) -> None:
        if not self._path:
            return
        try:
            path = Path(self._path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save token {self.email}: {e}")


class TokenManager:
    """Manages the pool of tokens with load balancing and lifecycle management."""

    def __init__(self, token_dir: str = "", proxy: str = "",
                 turnstile_solver_url: str = ""):
        self.token_dir = token_dir or str(Path(__file__).parent.parent / "web_token")
        self.proxy = proxy
        self.turnstile_solver_url = turnstile_solver_url
        self._tokens: List[TokenInfo] = []
        self._index = 0  # for round-robin
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> List[TokenInfo]:
        return self._tokens

    def load(self) -> int:
        """Load all token files from web_token/ directory."""
        token_path = Path(self.token_dir)
        if not token_path.exists():
            logger.warning(f"Token directory not found: {self.token_dir}")
            return 0

        loaded = 0
        for f in sorted(token_path.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                token = TokenInfo.from_dict(data, path=str(f))
                # Ensure device_id and session_id
                if not token.device_id:
                    token.device_id = str(uuid.uuid4())
                if not token.session_id:
                    token.session_id = str(uuid.uuid4())
                self._tokens.append(token)
                loaded += 1
            except Exception as e:
                logger.error(f"Failed to load token file {f.name}: {e}")

        logger.info(f"Loaded {loaded} tokens from {self.token_dir}")
        return loaded

    def add_token(self, token: TokenInfo) -> None:
        """Add a new token to the pool."""
        if not token._path:
            token._path = str(Path(self.token_dir) / f"{token.email.replace('@', '_at_').replace('.', '_')}.json")
        self._tokens.append(token)
        token.save()
        logger.info(f"Added token: {token.email} (status={token.status.value})")

    def remove_token(self, email: str) -> bool:
        """Remove a token from the pool and delete its file."""
        for i, t in enumerate(self._tokens):
            if t.email == email:
                self._tokens.pop(i)
                if t._path:
                    try:
                        Path(t._path).unlink(missing_ok=True)
                    except Exception:
                        pass
                logger.info(f"Removed token: {email}")
                return True
        return False

    def _weight_for_token(self, token: TokenInfo) -> float:
        """Higher weight for newer tokens based on registered_at."""
        if not token.registered_at:
            return 1.0
        try:
            from datetime import datetime, timezone
            reg = datetime.fromisoformat(token.registered_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age_hours = max(0, (now - reg).total_seconds() / 3600)
            # Newer tokens get higher weight: 24h -> ~12, 0h -> 24
            return max(1.0, 24.0 / (age_hours + 1))
        except Exception:
            return 1.0

    def get_available(self) -> Optional[TokenInfo]:
        """Get the next available token (round-robin / weighted-random / least-used)."""
        available = [t for t in self._tokens if t.is_available]
        if not available:
            return None

        import random

        load_balance = get_config("token.load_balance", "round-robin")
        if load_balance == "random":
            return random.choice(available)
        elif load_balance == "least-used":
            return min(available, key=lambda t: t.use_count)
        elif load_balance == "weighted-random":
            weights = [self._weight_for_token(t) for t in available]
            total = sum(weights)
            r = random.uniform(0, total)
            cum = 0.0
            for token, w in zip(available, weights):
                cum += w
                if r <= cum:
                    return token
            return available[-1]
        else:
            # round-robin
            self._index = self._index % len(available)
            token = available[self._index]
            self._index += 1
            return token

    def get_by_email(self, email: str) -> Optional[TokenInfo]:
        """Get a token by email."""
        for t in self._tokens:
            if t.email == email:
                return t
        return None

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._tokens if t.status in (TokenStatus.ACTIVE, TokenStatus.STALE) and t.access_token)

    @property
    def total_count(self) -> int:
        return len(self._tokens)

    def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        by_status = {}
        for t in self._tokens:
            s = t.status.value
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "total": self.total_count,
            "active": self.active_count,
            "by_status": by_status,
        }

    # ---------- Token Refresh ----------

    async def refresh_token(self, token: TokenInfo) -> bool:
        """Refresh an expired access_token using refresh_token."""
        if not token.refresh_token:
            logger.warning(f"No refresh_token for {token.email}")
            return False

        try:
            from curl_cffi import requests as curl_requests

            TOKEN_URL = "https://auth.openai.com/oauth/token"
            client_id = token.client_id or "app_2SKx67EdpoN0G6j64rFvigXD"

            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

            resp = curl_requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": token.refresh_token,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                proxies=proxies,
                impersonate="chrome131",
                timeout=30,
            )

            if resp.status_code != 200:
                error_text = resp.text[:200]
                logger.error(f"Token refresh failed for {token.email}: {resp.status_code} {error_text}")
                token.record_failure(FailReason.TOKEN_EXPIRED)
                return False

            data = resp.json()
            new_access = str(data.get("access_token") or "").strip()
            new_refresh = str(data.get("refresh_token") or "").strip()
            new_id_token = str(data.get("id_token") or "").strip()
            expires_in = int(data.get("expires_in", 3600))

            if not new_access:
                logger.error(f"Refresh returned empty access_token for {token.email}")
                token.record_failure(FailReason.TOKEN_EXPIRED)
                return False

            token.access_token = new_access
            if new_refresh:
                token.refresh_token = new_refresh
            if new_id_token:
                token.id_token = new_id_token

            now = int(time.time())
            token.last_refresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            token.expired = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + expires_in))
            token.status = TokenStatus.ACTIVE
            token.fail_count = 0
            token.last_fail_reason = None
            token.save()

            logger.info(f"Token refreshed: {token.email}")
            return True

        except Exception as e:
            logger.error(f"Token refresh error for {token.email}: {e}")
            token.record_failure(FailReason.TOKEN_EXPIRED)
            return False

    # ---------- Scheduled Tasks ----------

    async def refresh_expired_tokens(self) -> int:
        """Refresh all (non-dead, non-disabled) tokens. Returns count of successfully refreshed."""
        refreshed = 0
        for token in self._tokens:
            if token.status in (TokenStatus.DEAD, TokenStatus.DISABLED):
                continue
            if not token.refresh_token:
                continue
            if await self.refresh_token(token):
                refreshed += 1
        return refreshed

    async def check_cooling_tokens(self) -> int:
        """Check if any cooling tokens can be reactivated."""
        recovered = 0
        now = time.time()
        for token in self._tokens:
            if token.status == TokenStatus.COOLING:
                if token.cooldown_until and now >= token.cooldown_until:
                    token.status = TokenStatus.ACTIVE
                    token.cooldown_until = None
                    token.fail_count = 0
                    token.save()
                    recovered += 1
                    logger.info(f"Token recovered from cooling: {token.email}")
        return recovered

    async def cleanup_dead_tokens(self) -> int:
        """Remove dead tokens older than dead_retain_hours."""
        retain_hours = get_config("token.dead_retain_hours", 24)
        cutoff = time.time() - retain_hours * 3600
        removed = 0
        to_remove = []
        for token in self._tokens:
            if token.status == TokenStatus.DEAD:
                if token.last_fail_at and token.last_fail_at < cutoff:
                    to_remove.append(token.email)
        for email in to_remove:
            if self.remove_token(email):
                removed += 1
        return removed

    async def scan_new_tokens(self) -> int:
        """Scan web_token/ directory for new token files not yet in pool."""
        token_path = Path(self.token_dir)
        if not token_path.exists():
            return 0

        existing_emails = {t.email for t in self._tokens}
        added = 0

        for f in sorted(token_path.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                email = data.get("email", "")
                if email and email not in existing_emails:
                    token = TokenInfo.from_dict(data, path=str(f))
                    if not token.device_id:
                        token.device_id = str(uuid.uuid4())
                    if not token.session_id:
                        token.session_id = str(uuid.uuid4())
                    self._tokens.append(token)
                    existing_emails.add(email)
                    added += 1
                    logger.info(f"New token discovered: {email}")
            except Exception as e:
                logger.error(f"Failed to scan token file {f.name}: {e}")

        return added

    def classify_error(self, status_code: int, body: str = "") -> FailReason:
        """Classify an API error into a FailReason."""
        if status_code == 401:
            return FailReason.TOKEN_EXPIRED
        if status_code == 403:
            body_lower = body.lower()
            if "banned" in body_lower or "suspended" in body_lower:
                return FailReason.BANNED
            if "cloudflare" in body_lower or "challenge" in body_lower:
                return FailReason.CF_CHALLENGE
            if "unusual activity" in body_lower:
                # POW not solved or sentinel token issue — retryable, not banned
                return FailReason.UNKNOWN
            return FailReason.BANNED
        if status_code == 429:
            return FailReason.RATE_LIMITED
        if status_code == 451:
            return FailReason.QUOTA_EXHAUSTED
        # Check body for specific patterns
        body_lower = body.lower()
        if "account has been disabled" in body_lower or "banned" in body_lower:
            return FailReason.BANNED
        if "quota" in body_lower or "limit exceeded" in body_lower:
            return FailReason.QUOTA_EXHAUSTED
        if "token expired" in body_lower or "invalid token" in body_lower or "token_invalidated" in body_lower:
            return FailReason.TOKEN_EXPIRED
        return FailReason.UNKNOWN
