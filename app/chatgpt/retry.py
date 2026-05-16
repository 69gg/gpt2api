"""Retry helper for flaky network requests (TLS errors, timeouts, etc.)."""
from __future__ import annotations

import asyncio
import time
from functools import wraps
from typing import Any, Callable, Coroutine, Type, Tuple

from loguru import logger


def retry_request(
    max_retries: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
    label: str = "",
) -> Callable:
    """Decorator: retry a sync function on specified exceptions.

    Args:
        max_retries: Maximum number of retry attempts (0 = no retry).
        delay: Initial delay between retries in seconds.
        backoff: Multiplier applied to delay after each retry.
        retry_on: Tuple of exception types to retry on.
        label: Optional label for log messages.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc = None
            cur_delay = delay
            for attempt in range(1 + max_retries):
                try:
                    return fn(*args, **kwargs)
                except retry_on as e:
                    last_exc = e
                    if attempt < max_retries:
                        tag = f"[{label}] " if label else ""
                        logger.debug(f"{tag}Retry {attempt+1}/{max_retries} after {type(e).__name__}: {e}")
                        time.sleep(cur_delay)
                        cur_delay *= backoff
                    else:
                        raise
            raise last_exc  # should not reach here
        return wrapper
    return decorator


def retry_call(
    fn: Callable,
    *args: Any,
    max_retries: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
    label: str = "",
    **kwargs: Any,
) -> Any:
    """Imperative retry wrapper — call sync fn with retries."""
    return retry_request(
        max_retries=max_retries,
        delay=delay,
        backoff=backoff,
        retry_on=retry_on,
        label=label,
    )(fn)(*args, **kwargs)


async def async_retry_call(
    fn: Callable[..., Coroutine[Any, Any, Any]],
    *args: Any,
    max_retries: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
    label: str = "",
    **kwargs: Any,
) -> Any:
    """Async retry wrapper — call async fn with non-blocking retries."""
    cur_delay = delay
    for attempt in range(1 + max_retries):
        try:
            return await fn(*args, **kwargs)
        except retry_on as e:
            if attempt < max_retries:
                tag = f"[{label}] " if label else ""
                logger.debug(f"{tag}Retry {attempt+1}/{max_retries} after {type(e).__name__}: {e}")
                await asyncio.sleep(cur_delay)
                cur_delay *= backoff
            else:
                raise
    raise RuntimeError("unreachable")
