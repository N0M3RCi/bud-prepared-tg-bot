from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Optional

from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError

from app.config import settings

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
BASE_RETRY_DELAY = 1.0  # seconds
IMAGE_RETRIES = 2  # fewer retries for image gen — fail fast


class LLMClient:
    """Wrapper around AsyncOpenAI configured for NEAR AI Cloud."""

    _instance: Optional[AsyncOpenAI] = None
    _image_instance: Optional[AsyncOpenAI] = None
    _default_tools: Optional[list[dict[str, Any]]] = None

    @classmethod
    def get_client(cls) -> AsyncOpenAI:
        if cls._instance is None:
            api_key = settings.near_ai_api_key
            if not api_key:
                logger.warning("NEAR_AI_API_KEY not set — using placeholder. Set your key in .env")
                api_key = "sk-placeholder"
            cls._instance = AsyncOpenAI(
                base_url=settings.near_ai_base_url,
                api_key=api_key,
                max_retries=0,  # We handle retries ourselves with backoff
            )
        return cls._instance

    @classmethod
    def get_image_client(cls) -> AsyncOpenAI:
        """Get or create the image generation client.

        Uses a separate base URL and API key if configured,
        otherwise falls back to the main NEAR AI endpoint and key.
        """
        if cls._image_instance is None:
            api_key = settings.near_ai_image_api_key or settings.near_ai_api_key
            if not api_key:
                logger.warning("NEAR_AI_API_KEY not set — using placeholder. Set your key in .env")
                api_key = "sk-placeholder"
            cls._image_instance = AsyncOpenAI(
                base_url=settings.near_ai_image_base_url or settings.near_ai_base_url,
                api_key=api_key,
                max_retries=0,
            )
        return cls._image_instance

    @classmethod
    def set_default_tools(cls, tools: list[dict[str, Any]]) -> None:
        cls._default_tools = tools

    @classmethod
    async def chat_completion_stream(
        cls,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Stream a chat completion, yielding raw text chunks (not JSON-wrapped)."""
        client = cls.get_client()
        kwargs: dict[str, Any] = {
            "model": model or settings.near_ai_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or settings.max_response_tokens,
            "stream": True,
        }
        if tools or cls._default_tools:
            kwargs["tools"] = tools or cls._default_tools
            kwargs["tool_choice"] = "auto"

        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                stream = await client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    delta = chunk.model_dump(mode="json")
                    choices = delta.get("choices", [])
                    if not choices:
                        continue
                    yield ("delta", delta)
                return
            except (APIError, RateLimitError, APITimeoutError) as e:
                last_exc = e
                if attempt < MAX_RETRIES - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning("LLM API error (attempt %d/%d): %s. Retrying in %.1fs...",
                                   attempt + 1, MAX_RETRIES, e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.error("LLM API failed after %d retries: %s", MAX_RETRIES, e)
            except Exception as e:
                logger.error("LLM streaming failed: %s", e)
                yield ("error", str(e))
                return

        yield ("error", f"LLM request failed after {MAX_RETRIES} attempts: {last_exc}")

    @classmethod
    async def stream_and_collect(
        cls,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> tuple[str, list[tuple[str, str, dict[str, Any]]]]:
        """Single streaming call that yields content tokens AND detects tool calls.

        Accumulates both the full text response and any tool_calls from the
        stream. Returns (full_text, tool_calls_list).

        This eliminates the need for a separate non-streaming pre-check call,
        cutting latency by one full LLM round-trip when no tools are invoked.
        """
        client = cls.get_client()
        kwargs: dict[str, Any] = {
            "model": model or settings.near_ai_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or settings.max_response_tokens,
            "stream": True,
        }
        if tools or cls._default_tools:
            kwargs["tools"] = tools or cls._default_tools
            kwargs["tool_choice"] = "auto"

        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                stream = await client.chat.completions.create(**kwargs)

                full_text = ""
                tool_calls_acc: dict[int, dict[str, Any]] = {}

                async for chunk in stream:
                    delta = chunk.model_dump(mode="json")
                    choices = delta.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    msg_delta = choice.get("delta", {})

                    # Accumulate content
                    text_piece = msg_delta.get("content") or ""
                    full_text += text_piece

                    # Accumulate tool calls from stream deltas
                    raw_tcs = msg_delta.get("tool_calls")
                    if raw_tcs:
                        for tc in raw_tcs:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "",
                                    "function": {"name": "", "arguments": ""},
                                }
                            entry = tool_calls_acc[idx]
                            if tc.get("id"):
                                entry["id"] = tc["id"]
                            if tc.get("function"):
                                fn = tc["function"]
                                if fn.get("name"):
                                    entry["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    entry["function"]["arguments"] += fn["arguments"]

                # Convert accumulated tool calls to standard format
                parsed_tool_calls: list[tuple[str, str, dict[str, Any]]] = []
                for idx in sorted(tool_calls_acc.keys()):
                    tc = tool_calls_acc[idx]
                    tc_id = tc["id"]
                    func_name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    parsed_tool_calls.append((tc_id, func_name, args))

                return full_text, parsed_tool_calls

            except (APIError, RateLimitError, APITimeoutError) as e:
                last_exc = e
                if attempt < MAX_RETRIES - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning("LLM API error (attempt %d/%d): %s. Retrying in %.1fs...",
                                   attempt + 1, MAX_RETRIES, e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.error("LLM API failed after %d retries: %s", MAX_RETRIES, e)
            except Exception as e:
                logger.error("LLM streaming failed: %s", e)
                return "", [("", "error", {"message": str(e)})]

        return "", [("", "error", {"message": f"LLM failed after {MAX_RETRIES} attempts: {last_exc}"})]

    @classmethod
    async def chat_completion(
        cls,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Non-streaming completion with retry — used for internal tool-call rounds."""
        client = cls.get_client()
        kwargs: dict[str, Any] = {
            "model": model or settings.near_ai_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or settings.max_response_tokens,
        }
        if tools or cls._default_tools:
            kwargs["tools"] = tools or cls._default_tools
            kwargs["tool_choice"] = "auto"

        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.chat.completions.create(**kwargs)
                return response.model_dump(mode="json")
            except (APIError, RateLimitError, APITimeoutError) as e:
                last_exc = e
                if attempt < MAX_RETRIES - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning("LLM API error (attempt %d/%d): %s. Retrying in %.1fs...",
                                   attempt + 1, MAX_RETRIES, e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.error("LLM API failed after %d retries: %s", MAX_RETRIES, e)
            except Exception as e:
                logger.error("LLM API call failed: %s", e)
                raise

        raise last_exc or RuntimeError("LLM request failed")

    @classmethod
    async def generate_image(cls, prompt: str) -> Optional[str]:
        """Generate an image via the configured image model.

        Uses the OpenAI-compatible images/generations endpoint directly
        with minimal retries for fast failure.
        """
        client = cls.get_image_client()

        for attempt in range(IMAGE_RETRIES):
            try:
                response = await client.images.generate(
                    model=settings.near_ai_image_model,
                    prompt=prompt,
                    n=1,
                    size=settings.near_ai_image_size,
                )
                data = response.model_dump(mode="json")
                url = data["data"][0].get("url") if data.get("data") else None
                if url:
                    return url
                # Some providers return b64_json instead of url
                b64 = data["data"][0].get("b64_json") if data.get("data") else None
                if b64:
                    return f"data:image/png;base64,{b64}"
                break  # No URL and no base64 — give up
            except Exception as e:
                if attempt < IMAGE_RETRIES - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning("Image gen attempt %d/%d failed, retry in %.1fs: %s",
                                   attempt + 1, IMAGE_RETRIES, delay, e)
                    await asyncio.sleep(delay)
                else:
                    logger.error("Image gen failed after %d attempts: %s", IMAGE_RETRIES, e)

        return None

    @classmethod
    async def count_tokens(cls, text: str) -> int:
        """Approximate token count using tiktoken if available, fallback to heuristic."""
        try:
            import tiktoken  # type: ignore[import-untyped]
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            # ~4 chars per token is a reasonable approximation
            return max(1, len(text) // settings.token_estimation_chars_per_token)