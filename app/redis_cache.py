from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REDIS_AVAILABLE = False
_redis_client = None

REDIS_URL = os.environ.get("REDIS_URL", "")


def _connect():
    global _redis_client, _REDIS_AVAILABLE
    if not REDIS_URL:
        _REDIS_AVAILABLE = False
        return
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3)
        _REDIS_AVAILABLE = True
        logger.info("Redis cache connected: %s", REDIS_URL[:30] + "...")
    except Exception as e:
        _REDIS_AVAILABLE = False
        logger.warning("Redis not available: %s", e)


_connect()

_DEFAULT_TTL = 300  # 5 minutes

# Fallback in-memory cache when Redis is unavailable
_fallback_cache: dict[str, tuple[Any, float]] = {}
_FALLBACK_TTL = 120  # 2 minutes fallback


async def get(key: str) -> Optional[Any]:
    """Get a value from cache. Returns None if not found."""
    if _REDIS_AVAILABLE and _redis_client:
        try:
            val = await _redis_client.get(key)
            if val is not None:
                return json.loads(val)
        except Exception:
            pass

    return _fallback_get(key)


async def set(key: str, value: Any, ttl: int = _DEFAULT_TTL) -> None:
    """Set a value in cache with TTL."""
    try:
        dumped = json.dumps(value)
    except (TypeError, ValueError):
        return

    if _REDIS_AVAILABLE and _redis_client:
        try:
            await _redis_client.setex(key, ttl, dumped)
            return
        except Exception:
            pass

    _fallback_set(key, value, ttl)


async def delete(key: str) -> None:
    """Delete a key from cache."""
    if _REDIS_AVAILABLE and _redis_client:
        try:
            await _redis_client.delete(key)
        except Exception:
            pass
    _fallback_cache.pop(key, None)


async def exists(key: str) -> bool:
    """Check if a key exists in cache."""
    if _REDIS_AVAILABLE and _redis_client:
        try:
            return bool(await _redis_client.exists(key))
        except Exception:
            pass
    return key in _fallback_cache


def _fallback_get(key: str) -> Optional[Any]:
    import time
    entry = _fallback_cache.get(key)
    if entry and time.monotonic() - entry[1] < _FALLBACK_TTL:
        return entry[0]
    _fallback_cache.pop(key, None)
    return None


def _fallback_set(key: str, value: Any, ttl: int = _FALLBACK_TTL) -> None:
    import time
    _fallback_cache[key] = (value, time.monotonic())
    if len(_fallback_cache) > 10000:
        # Prune expired
        now = time.monotonic()
        expired = [k for k, v in _fallback_cache.items() if now - v[1] > _FALLBACK_TTL * 2]
        for k in expired[:2000]:
            _fallback_cache.pop(k, None)


async def close():
    if _redis_client:
        try:
            await _redis_client.close()
        except Exception:
            pass
