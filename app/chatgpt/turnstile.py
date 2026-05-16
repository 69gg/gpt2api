"""
Turnstile VM solver — placeholder for OpenAI's custom Turnstile challenge.

The full VM solver implementation (decompiler + VM executor) is complex
and will be ported from realasfngl/ChatGPT's Python implementation.

For now, this module provides:
- Integration with external Turnstile solver services
- Fallback to single-step chat-requirements when Turnstile is required
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from curl_cffi.requests import AsyncSession
from loguru import logger


class TurnstileSolver:
    """Base class for Turnstile solvers."""

    async def solve(self, dx: str, url: str = "https://chatgpt.com") -> Optional[str]:
        """Solve a Turnstile challenge. Returns the token string or None."""
        raise NotImplementedError


class ExternalSolver(TurnstileSolver):
    """Use an external Turnstile solver service (e.g., api_solver.py from grok2api)."""

    def __init__(self, solver_url: str, timeout: int = 60):
        self.solver_url = solver_url.rstrip("/")
        self.timeout = timeout

    async def solve(self, dx: str, url: str = "https://chatgpt.com") -> Optional[str]:
        try:
            async with AsyncSession(timeout=20) as session:
                resp = await session.post(
                    f"{self.solver_url}/turnstile",
                    json={"url": url, "sitekey": dx},
                )
                if resp.status_code != 200:
                    logger.error(f"Turnstile solver returned {resp.status_code}")
                    return None

                data = resp.json()
                task_id = data.get("taskId")
                if not task_id:
                    # Maybe direct response
                    token = data.get("token") or data.get("solution", {}).get("token", "")
                    if token:
                        return token
                    return None

                # Poll for result
                for _ in range(self.timeout // 2):
                    await asyncio.sleep(2)
                    try:
                        r = await session.get(
                            f"{self.solver_url}/result",
                            params={"id": task_id},
                        )
                        if r.status_code == 200:
                            rd = r.json()
                            solution = rd.get("solution", {})
                            token = solution.get("token") or solution.get("value", "")
                            if token == "CAPTCHA_FAIL":
                                logger.error("Turnstile solver: CAPTCHA_FAIL")
                                return None
                            if token and token != "done":
                                return token
                            if token == "done":
                                # cf_clearance mode, not turnstile token
                                return None
                    except Exception as e:
                        logger.debug(f"Turnstile poll error: {e}")

                logger.error("Turnstile solver timeout")
                return None
        except Exception as e:
            logger.error(f"Turnstile solver error: {e}")
            return None


class CapsolverSolver(TurnstileSolver):
    """Use capsolver.com API for Turnstile solving."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def solve(self, dx: str, url: str = "https://chatgpt.com") -> Optional[str]:
        try:
            async with AsyncSession(timeout=30) as session:
                # Create task
                resp = await session.post(
                    "https://api.capsolver.com/createTask",
                    json={
                        "clientKey": self.api_key,
                        "task": {
                            "type": "AntiTurnstileTaskProxyLess",
                            "websiteURL": url,
                            "websiteKey": dx,
                        },
                    },
                )
                data = resp.json()
                task_id = data.get("taskId")
                if not task_id:
                    logger.error(f"Capsolver createTask failed: {data}")
                    return None

                # Poll for result
                for _ in range(30):
                    await asyncio.sleep(2)
                    r = await session.post(
                        "https://api.capsolver.com/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rd = r.json()
                    status = rd.get("status", "")
                    if status == "ready":
                        token = rd.get("solution", {}).get("token", "")
                        if token:
                            return token
                        return None
                    if status == "failed":
                        logger.error(f"Capsolver task failed: {rd}")
                        return None

                return None
        except Exception as e:
            logger.error(f"Capsolver error: {e}")
            return None


# TODO: Implement VMSolver (bytecode decompiler + executor) from realasfngl/ChatGPT
# class VMSolver(TurnstileSolver):
#     """Pure Python VM-based Turnstile solver (no external service needed)."""
#     def __init__(self):
#         self.decompiler = Decompiler()
#         self.vm = VMExecutor()
#
#     async def solve(self, dx: str, url: str = "https://chatgpt.com") -> Optional[str]:
#         # 1. Fetch Turnstile bytecode from dx
#         # 2. Decompile bytecode → extract XOR keys + fingerprint requirements
#         # 3. Build browser fingerprint payload
#         # 4. Execute VM logic → encrypt payload
#         # 5. Return the turnstile token
#         pass


def create_solver(solver_url: str = "", capsolver_key: str = "") -> Optional[TurnstileSolver]:
    """Factory function to create the appropriate Turnstile solver."""
    if solver_url:
        return ExternalSolver(solver_url)
    if capsolver_key:
        return CapsolverSolver(capsolver_key)
    return None
