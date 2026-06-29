from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from app.database import Database
from app.llm_client import LLMClient

logger = logging.getLogger(__name__)

# Centralized in-memory cache: {user_id: {settings dict, _ts}}
_user_cache: dict[int, dict[str, Any]] = {}
_USER_CACHE_TTL = 60  # seconds
_MAX_CACHE_SIZE = 2000


def _get_cached_user(user_id: int) -> dict[str, Any] | None:
    entry = _user_cache.get(user_id)
    if entry and time.monotonic() - entry["_ts"] < _USER_CACHE_TTL:
        return entry
    _user_cache.pop(user_id, None)
    return None


def _set_cached_user(user_id: int, data: dict[str, Any]) -> None:
    data["_ts"] = time.monotonic()
    _user_cache[user_id] = data
    if len(_user_cache) > _MAX_CACHE_SIZE:
        oldest_keys = sorted(_user_cache.keys(), key=lambda k: _user_cache[k]["_ts"])[:400]
        for k in oldest_keys:
            _user_cache.pop(k, None)


def bust_cache(user_id: int) -> None:
    _user_cache.pop(user_id, None)


class SessionManager:
    """Manages per-user conversation sessions with sliding window memory."""

    # Per-conversation locks to prevent race conditions during compression
    _compress_locks: dict[int, asyncio.Lock] = {}

    @classmethod
    async def get_or_create_session(cls, user_id: int, username: Optional[str] = None,
                                     first_name: Optional[str] = None,
                                     last_name: Optional[str] = None,
                                     language_code: Optional[str] = None) -> dict:
        user = await Database.get_or_create_user(
            user_id, username, first_name, last_name, language_code,
        )
        bust_cache(user_id)
        return user

    @classmethod
    async def get_system_prompt(cls, user_id: int) -> str:
        user = await cls._get_user_settings_cached(user_id)
        return user.get("system_prompt", "You are a helpful AI assistant.") if user else "You are a helpful AI assistant."

    @classmethod
    async def get_user_max_messages(cls, user_id: int) -> int:
        user = await cls._get_user_settings_cached(user_id)
        return (user or {}).get("max_context_messages", 50)

    @classmethod
    async def get_user_max_tokens(cls, user_id: int) -> int:
        user = await cls._get_user_settings_cached(user_id)
        return (user or {}).get("max_context_tokens", 128000)

    @classmethod
    async def _get_user_settings_cached(cls, user_id: int) -> Optional[dict]:
        cached = _get_cached_user(user_id)
        if cached:
            return cached
        user = await Database.get_user_settings(user_id)
        if user:
            _set_cached_user(user_id, user)
        return user

    @classmethod
    async def get_preferred_model_cached(cls, user_id: int) -> Optional[str]:
        """Get preferred model from cached user settings — avoids a DB call."""
        user = await cls._get_user_settings_cached(user_id)
        return user.get("preferred_model") if user else None

    @classmethod
    async def update_system_prompt(cls, user_id: int, prompt: str) -> None:
        await Database.update_user_prompt(user_id, prompt)
        bust_cache(user_id)

    @classmethod
    async def get_conversation_context(cls, user_id: int) -> tuple[list[dict[str, Any]], int]:
        """Build the context messages array for an LLM call.

        1. System prompt
        2. Optional context summary (if conversation has been compressed)
        3. Recent messages (sliding window)

        Returns:
            (context_messages, conversation_id)
        """
        system_prompt = await cls.get_system_prompt(user_id)
        max_messages = await cls.get_user_max_messages(user_id)
        max_tokens = await cls.get_user_max_tokens(user_id)

        # Ensure the user exists in the database before creating conversations
        # (FK: conversations.user_id → users.user_id)
        await Database.get_or_create_user(user_id)

        conversation = await Database.get_active_conversation(user_id)
        if not conversation:
            conv_id = await Database.create_conversation(user_id)
            conversation = {"id": conv_id}

        messages = await Database.get_conversation_messages(
            conversation["id"],
            limit=max_messages,
            token_budget=max_tokens,
        )

        context: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

        # Check if there's a context summary (compressed from earlier pruning)
        summary_msg = await Database.get_latest_summary(conversation["id"])
        if summary_msg:
            context.append({"role": "system", "content": f"[Previous conversation summary]: {summary_msg}"})

        for msg in messages:
            entry: dict[str, Any] = {
                "role": msg["role"],
                "content": msg["content"],
            }
            if msg.get("tool_call_id"):
                entry["tool_call_id"] = msg["tool_call_id"]
            if msg.get("tool_name"):
                entry["name"] = msg["tool_name"]
            # Reconstruct tool_calls for assistant messages that invoked tools
            if msg.get("tool_calls_json"):
                tool_calls_data = json.loads(msg["tool_calls_json"])
                if tool_calls_data:
                    entry["tool_calls"] = tool_calls_data
            context.append(entry)

        return context, conversation["id"]

    @classmethod
    async def add_to_conversation(
        cls,
        conversation_id: int,
        user_id: int,
        role: str,
        content: str,
        tool_call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_calls_json: Optional[str] = None,
    ) -> None:
        token_count = await LLMClient.count_tokens(content)
        await Database.add_message(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            token_count=token_count,
            tool_calls_json=tool_calls_json,
        )

    @classmethod
    async def save_assistant_response(cls, conversation_id: int, user_id: int,
                                       content: str,
                                       tool_calls_json: Optional[str] = None) -> None:
        await cls.add_to_conversation(
            conversation_id, user_id, "assistant", content,
            tool_calls_json=tool_calls_json,
        )

    @classmethod
    async def save_tool_result(cls, conversation_id: int, user_id: int,
                                tool_call_id: str, tool_name: str, result: str) -> None:
        await cls.add_to_conversation(
            conversation_id, user_id, "tool", result,
            tool_call_id=tool_call_id, tool_name=tool_name,
        )

    @classmethod
    async def clear_conversation(cls, user_id: int) -> None:
        conversation = await Database.get_active_conversation(user_id)
        if conversation:
            await Database.delete_conversation(conversation["id"])

    @classmethod
    async def compress_context(cls, user_id: int, conversation_id: int) -> None:
        """Compress old messages into a summary, reducing context size.

        Fetches ALL messages (no limit), keeps the most recent 10,
        and summarizes the rest. The summarized messages are then
        deleted from the database. Protected by a per-conversation lock
        to prevent race conditions with concurrent requests.
        """
        # Acquire per-conversation lock to prevent race conditions
        lock = cls._compress_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            try:
                # CRITICAL FIX: Do NOT use limit here — fetch ALL messages so
                # nothing gets silently dropped during compression.
                messages = await Database.get_conversation_messages(
                    conversation_id, limit=None, token_budget=None,
                )

                if len(messages) <= 15:
                    return  # Not worth compressing yet

                # Keep last 10, summarize the rest
                summarize = messages[:-10]

                summary_text = "\n".join(
                    f"[{m['role']}]: {m['content'][:200]}"
                    for m in summarize
                )

                try:
                    response = await LLMClient.chat_completion(
                        messages=[
                            {"role": "system", "content": "Summarize the key points of this conversation concisely. Focus on facts, decisions, and user preferences."},
                            {"role": "user", "content": f"Summarize this conversation:\n\n{summary_text}"},
                        ],
                        temperature=0.2,
                        max_tokens=512,
                    )
                    summary = response.get("choices", [{}])[0].get("message", {}).get("content", "")
                except Exception:
                    summary = ""

                if not summary:
                    return

                # Store the summary
                await Database.save_conversation_summary(conversation_id, summary)

                # Delete summarized messages (keep only the most recent 10)
                pool = await Database.get_pool()
                async with pool.acquire() as conn:
                    if summarize:
                        ids_to_delete = [m["id"] for m in summarize]
                        await conn.execute(
                            "DELETE FROM messages WHERE id = ANY($1::bigint[])",
                            ids_to_delete,
                        )
            finally:
                # Clean up lock to prevent memory leak
                cls._compress_locks.pop(conversation_id, None)