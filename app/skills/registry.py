from __future__ import annotations

import json
import logging
from typing import Any

from app.skills.web_search import TOOL_DEFINITION as SEARCH_TOOL, web_search
from app.skills.image_gen import TOOL_DEFINITION as IMAGE_TOOL, generate_image
from app.skills.file_reader import TOOL_DEFINITION as FILE_TOOL, read_file
from app.skills.calculator import TOOL_DEFINITION as CALC_TOOL, calculate

logger = logging.getLogger(__name__)

# Registry: maps tool name -> async callable
_TOOL_HANDLERS: dict[str, Any] = {
    "web_search": web_search,
    "generate_image": generate_image,
    "read_file": read_file,
    "calculate": calculate,
}

# All tool definitions to send to the LLM
ALL_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    SEARCH_TOOL,
    IMAGE_TOOL,
    FILE_TOOL,
    CALC_TOOL,
]


async def execute_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool by name with the given arguments and return the result string."""
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        logger.warning("Unknown tool called: %s", tool_name)
        return f"Error: Unknown tool '{tool_name}'."

    logger.info("Executing tool: %s with args: %s", tool_name, arguments)
    try:
        result = await handler(**arguments)
        return str(result) if result is not None else "Tool returned no result."
    except TypeError as e:
        logger.error("Tool %s argument error: %s", tool_name, e)
        return f"Error executing {tool_name}: Invalid arguments. {e}"
    except Exception as e:
        logger.error("Tool %s execution failed: %s", tool_name, e)
        return f"Error executing {tool_name}: {e}"


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return all tool definitions for the LLM to use."""
    return ALL_TOOL_DEFINITIONS


def parse_tool_calls(response: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Extract tool calls from an LLM response.

    Returns:
        List of (tool_call_id, tool_name, arguments_dict)
    """
    tool_calls = []
    choice = response.get("choices", [{}])[0]
    message = choice.get("message", {})
    calls = message.get("tool_calls") or message.get("toolCalls") or []

    for tc in calls:
        if isinstance(tc, dict):
            tc_id = tc.get("id", "")
            func = tc.get("function", {})
            name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_calls.append((tc_id, name, args))

    return tool_calls