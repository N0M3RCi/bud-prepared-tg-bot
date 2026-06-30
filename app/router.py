from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, CallbackQuery, ChatPermissions
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app.database import Database
from app.llm_client import LLMClient
from app.session_manager import SessionManager, bust_cache
from app.skills.registry import (
    get_tool_definitions,
    execute_tool,
)
from app.skills.image_gen import pop_pending_images, current_user_id as image_ctx_var

from app.menu import menu_router, process_pending_action, _pending_action as _pending_menu_action, _return_to_page
from app.dashboard_reporter import (
    report_message,
    report_token_spend,
    report_event,
    report_command,
    report_error,
    report_admin_action,
    report_tool_usage,
)

from app.onboarding import onboarding_router, _show_onboarding

logger = logging.getLogger(__name__)

router = Router()

# Include the premium menu module's router
router.include_router(menu_router)
# Include the onboarding router
router.include_router(onboarding_router)

# Per-user processing lock
_processing: set[int] = set()
# Last user message per user for /retry
_last_user_message: dict[int, str] = {}
# Update ID deduplication — prevents duplicate processing from Telegram webhook retries
_processed_updates: dict[tuple[int, int], float] = {}
_UPDATE_TTL = 300  # seconds — keep update IDs for 5 minutes

# Content-based dedup — catches late webhook retries that arrive after _processing
# is released. Tracks (user_id, text) -> timestamp. Retries always have the
# exact same text, so an identical text within the cooldown window is a retry.
_recent_messages: dict[tuple[int, str], float] = {}
_MESSAGE_TTL = 15  # seconds — how long to remember a processed message text
# Safety limit
MAX_TOOL_ITERATIONS = 10
# Compression threshold
COMPRESS_AFTER_MESSAGES = 40

# Bot start time (single source of truth, set from bot.py)
_bot_start_time: float = 0.0


def set_bot_start_time(t: float) -> None:
    global _bot_start_time
    _bot_start_time = t


# ─── Helpers ───

# Bot username cached at startup for group mention detection
_bot_username: str = ""

def set_bot_username(name: str) -> None:
    global _bot_username
    _bot_username = name


def _log_request(user_id: int, event: str, **extra: Any) -> None:
    extra_str = " ".join(f"{k}={v}" for k, v in extra.items())
    logger.info("[user=%d] %s %s", user_id, event, extra_str)


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def _is_group_chat(message: Message) -> bool:
    """Check if the message is from a group or supergroup chat."""
    return message.chat.type in ("group", "supergroup")


async def _should_respond_in_group(message: Message) -> bool:
    """Check whether the bot should respond in a group/supergroup chat.

    In groups, the bot responds when:
    - The bot is @mentioned in the message text
    - The message is a reply to the bot's own message (continues a thread)

    Command filtering is handled by restrict_group_commands_middleware.
    """
    if not _is_group_chat(message):
        return True  # Private chats always respond

    text = (message.text or message.caption or "").strip()
    if not text:
        return False

    # Check for @mention
    if _bot_username and f"@{_bot_username}" in text.lower():
        return True

    # Check if it's a reply to the bot (continuing a thread)
    if message.reply_to_message and message.reply_to_message.from_user:
        bot_me = await message.bot.me()
        if message.reply_to_message.from_user.id == bot_me.id:
            return True

    return False


def _escape_md(text: str) -> str:
    """Escape Telegram Markdown special characters in user-provided text.
    Prevents entity parsing errors (_ for italic, * for bold, ` for code)."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


def _is_safe_prompt(prompt: str) -> tuple[bool, str]:
    """Validate a system prompt for injection attempts. Returns (safe, reason)."""
    if len(prompt) > settings.max_system_prompt_length:
        return False, f"System prompt too long ({len(prompt)} chars, max {settings.max_system_prompt_length})"
    # Check for obvious jailbreak attempts
    lower = prompt.lower()
    dangerous_patterns = [
        "ignore all previous instructions",
        "ignore all prior instructions",
        "forget your previous instructions",
        "you are now ",
        "act as a different",
        "disregard your",
        "override your",
    ]
    for pattern in dangerous_patterns:
        if pattern in lower:
            return False, f"System prompt contains disallowed pattern: '{pattern}'"
    return True, ""


# ─── Pending passcode verification ───
_pending_passcode: dict[int, int] = {}  # user_id -> group_id

# Dedup: track last /start time per user to prevent double webhook delivery
_last_start_time: dict[int, float] = {}
_START_COOLDOWN = 4.0  # seconds


async def _handle_passcode_verification(user_id: int, text: str, message: Message) -> bool:
    """Check if the user has a pending passcode verification.
    Returns True if the message was consumed as a passcode."""
    group_id = _pending_passcode.get(user_id)
    if group_id is None:
        return False

    stored_passcode = await Database.get_group_passcode(group_id)
    if stored_passcode is None:
        _pending_passcode.pop(user_id, None)
        return False

    if text.strip() == stored_passcode:
        try:
            # Unrestrict the user in the group - grant all permissions
            await message.bot.restrict_chat_member(
                chat_id=group_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=True,
                    can_invite_users=True,
                    can_pin_messages=True,
                ),
            )
            await message.answer(
                "✅ Passcode correct! You now have full access to the group."
            )
            _log_request(user_id, "passcode_success", group=str(group_id))
        except Exception as e:
            logger.error("Failed to unrestrict user %d in group %d: %s", user_id, group_id, e)
            await message.answer("❌ Failed to verify passcode. Please contact an admin.")
        _pending_passcode.pop(user_id, None)
    else:
        await message.answer("❌ Incorrect passcode. Please try again, or contact an admin.")

    return True


# ─── Chat Member Updates (for passcode groups) ───


@router.message(F.new_chat_members)
async def handle_new_chat_members(message: Message) -> None:
    """Detect new members joining a group and enforce passcode if configured."""
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        return

    # Check if group has a passcode
    stored_passcode = await Database.get_group_passcode(chat.id)
    if stored_passcode is None:
        return

    for new_user in message.new_chat_members:
        # Skip bot users
        if new_user.is_bot:
            continue

        # Restrict the new member (mute them) until they provide the passcode
        try:
            await message.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=new_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=None,
            )
        except Exception as e:
            logger.error("Failed to restrict user %d in group %d: %s", new_user.id, chat.id, e)
            continue

        # Track this user for passcode verification
        _pending_passcode[new_user.id] = chat.id

        # Try to DM the user with the passcode prompt
        try:
            await message.bot.send_message(
                new_user.id,
                f"🔒 *Passcode Required*\n\n"
                f"Welcome! To access this group, please enter the passcode.\n\n"
                f"*Send the passcode as a reply to this message.*\n"
                f"If you don't have the passcode, contact the group admin.",
                parse_mode="Markdown",
            )
            logger.info("[user=%d] passcode_prompt group=%s", new_user.id, str(chat.id))
        except Exception as e:
            logger.warning("Failed to DM user %d for passcode: %s", new_user.id, e)
            _pending_passcode.pop(new_user.id, None)

async def _check_rate_limit(user_id: int, message: Message) -> bool:
    """Check rate limits. Returns True if the request should be blocked."""
    if _is_admin(user_id):
        return False  # No rate limit for admins

    # Check daily cap via persistent stats
    today_usage = await Database.get_user_usage_today(user_id)
    if today_usage["requests"] >= settings.rate_limit_daily_cap:
        asyncio.ensure_future(report_error(
            user_id=user_id,
            error_type="daily_cap_reached",
            details=f"Daily cap of {settings.rate_limit_daily_cap} requests reached for user {user_id}",
        ))
        await _reply_or_answer(message,
            f"You've reached your daily limit of {settings.rate_limit_daily_cap} requests. "
            "Please try again tomorrow."
        )
        return True

    # Check rate limit window
    allowed = await Database.check_rate_limit(user_id)
    if not allowed:
        remaining = await Database.get_rate_limit_remaining(user_id)
        asyncio.ensure_future(report_error(
            user_id=user_id,
            error_type="rate_limited",
            details=f"Rate limit ({settings.rate_limit_max_requests} req/{settings.rate_limit_window_seconds}s) for user {user_id}",
        ))
        await _reply_or_answer(message,
            f"You're sending too many requests. "
            f"Please wait {remaining} seconds before sending another request."
        )
        return True

    return False


# ─── Middleware ───

@router.message.middleware()
async def dedup_middleware(handler, event: Message, data: dict):
    """Drop duplicate updates from Telegram webhook retries.

    Telegram may retry a webhook POST if it doesn't receive a 200 response
    within its timeout (~20s). Since LLM calls often take 30s+, the retry
    arrives after the first handler finished, causing duplicate replies.
    We track update IDs with a TTL to drop retries.
    """
    now = time.time()
    # Use (chat_id, message_id) as unique key — Telegram retries send the
    # same update with identical chat+message IDs
    if event.chat and event.message_id:
        dedup_key = (event.chat.id, event.message_id)
        if dedup_key in _processed_updates:
            logger.debug("Dropping duplicate update chat=%s msg=%s (retry)", event.chat.id, event.message_id)
            return  # Drop the retry — already processed
        _processed_updates[dedup_key] = now
    else:
        dedup_key = None

    # Periodic cleanup of stale dedup entries
    if len(_processed_updates) > 1000:
        cutoff = now - _UPDATE_TTL
        stale = [uid for uid, ts in _processed_updates.items() if ts < cutoff]
        for uid in stale:
            del _processed_updates[uid]
    if len(_recent_messages) > 500:
        cutoff = now - _MESSAGE_TTL
        stale = [k for k, ts in _recent_messages.items() if ts < cutoff]
        for k in stale:
            del _recent_messages[k]

    return await handler(event, data)


@router.message.middleware()
async def dashboard_reporting_middleware(handler, event: Message, data: dict):
    """Report every incoming message to the dashboard (fire-and-forget).

    Placed first so it runs even if later middleware blocks the event.
    """
    if event.from_user and event.from_user.id:
        msg_text = event.text or event.caption or ""
        asyncio.ensure_future(report_message(
            user_id=event.from_user.id,
            first_name=event.from_user.first_name or "",
            last_name=event.from_user.last_name or "",
            username=event.from_user.username or "",
            language_code=event.from_user.language_code or "",
            chat_id=event.chat.id,
            chat_type=event.chat.type,
            text=msg_text,
        ))
    return await handler(event, data)


@router.message.middleware()
async def command_reporting_middleware(handler, event: Message, data: dict):
    """Report every command executed to the dashboard (fire-and-forget).

    Provides structured command data (command name + args) so the dashboard
    can track command usage analytics separately from general messages.
    """
    if event.text and event.text.startswith("/") and event.from_user:
        full_text = event.text
        parts = full_text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/")
        # Strip bot username from command (e.g. /ping@botusername)
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        args = parts[1].strip() if len(parts) > 1 else ""
        asyncio.ensure_future(report_command(
            user_id=event.from_user.id,
            command=cmd,
            args=args,
            first_name=event.from_user.first_name or "",
            last_name=event.from_user.last_name or "",
            username=event.from_user.username or "",
            chat_id=event.chat.id,
            chat_type=event.chat.type,
        ))
    return await handler(event, data)


# Whitelisted commands allowed in groups for non-admin users
_ALLOWED_GROUP_COMMANDS = {"ping", "model", "start", "help", "h", "commands", "menu", "m"}
# Admin-only commands
_ADMIN_COMMANDS = {"admin_stats", "admin_broadcast", "admin_users", "admin_block", "admin_unblock", "admin_feedback", "admin_clear_feedback", "admin_viewconv", "admin_llm_config", "admin_diag", "admin_analytics", "admin_skills"}


@router.message.middleware()
async def restrict_group_commands_middleware(handler, event: Message, data: dict):
    """Block non-whitelisted commands in groups for regular users.

    In groups, only @mention + chat is the intended interaction.
    Commands like /stats, /new, /search etc. are private-chat only.
    Admin commands tell non-admins they cannot be used.
    """
    if event.chat.type in ("group", "supergroup"):
        text = (event.text or "").strip()
        if text.startswith("/"):
            cmd = text.split()[0].lower().lstrip("/")
            # Remove bot username from command if present (e.g. /ping@botusername)
            if "@" in cmd:
                cmd = cmd.split("@")[0]

            # Allow whitelisted commands
            if cmd in _ALLOWED_GROUP_COMMANDS:
                return await handler(event, data)

            # Admin-only commands — tell non-admins
            if cmd in _ADMIN_COMMANDS:
                if event.from_user and event.from_user.id not in settings.admin_ids:
                    await event.reply(
                        "⚠️ This command can only be accessed by an admin."
                    )
                    return
                return await handler(event, data)

            # All other commands — tell user they're private-chat only
            if event.from_user:
                await event.reply(
                    f"⚠️ The /{cmd} command is only available in private chat. "
                    "Please send me a direct message to use it."
                )
            return
    return await handler(event, data)


@router.message.middleware()
async def latency_middleware(handler, event: Message, data: dict):
    start = time.monotonic()
    try:
        result = await handler(event, data)
        elapsed = (time.monotonic() - start) * 1000
        if elapsed > 1000:
            uid = event.from_user.id if event.from_user else 0
            logger.warning("[user=%d] Slow handler (%.0fms)", uid, elapsed)
        return result
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        uid = event.from_user.id if event.from_user else 0
        logger.error("[user=%d] Handler failed after %.0fms: %s", uid, elapsed, e)
        asyncio.ensure_future(report_error(
            user_id=uid or None,
            error_type="handler_error",
            details=f"Handler failed after {elapsed:.0f}ms: {type(e).__name__}: {e}",
        ))
        raise


# ─────────────────────────── Commands ───────────────────────────

# Aliases
CMD_START = Command("start")
CMD_HELP = Command("help", "h", "commands")
CMD_NEW = Command("new", "clear", "reset", "n")
CMD_PROMPT = Command("prompt", "system", "sys")
CMD_SETTINGS = Command("settings", "config")
CMD_STATS = Command("stats", "usage")
CMD_RETRY = Command("retry")
CMD_CHATID = Command("chatid", "id")
CMD_CONVERSATIONS = Command("conversations", "conv", "chats")
CMD_EXPORT = Command("export")
CMD_SWITCH = Command("switch")
CMD_RENAME = Command("rename")
CMD_DELETE_CONV = Command("delete_conv")
CMD_ADMIN_STATS = Command("admin_stats")
CMD_ADMIN_BROADCAST = Command("admin_broadcast")
CMD_ADMIN_USERS = Command("admin_users")
CMD_ADMIN_BLOCK = Command("admin_block")
CMD_ADMIN_UNBLOCK = Command("admin_unblock")

# New shortcut commands
CMD_SEARCH = Command("search")
CMD_IMAGINE = Command("imagine", "draw", "image")
CMD_CALC = Command("calc", "calculate", "math")
CMD_PING = Command("ping")
CMD_MODEL = Command("model")
CMD_FEEDBACK = Command("feedback", "suggest")
CMD_FIND = Command("find", "searchconv")
CMD_DELETE_MY_DATA = Command("delete_my_data")
CMD_ADMIN_FEEDBACK = Command("admin_feedback")
CMD_ADMIN_CLEAR_FEEDBACK = Command("admin_clear_feedback")

# Premium admin commands
CMD_ADMIN_VIEWCONV = Command("admin_viewconv")
CMD_ADMIN_LLM_CONFIG = Command("admin_llm_config")
CMD_ADMIN_DIAG = Command("admin_diag")
CMD_ADMIN_ANALYTICS = Command("admin_analytics")
CMD_ADMIN_SKILLS = Command("admin_skills")


@router.message(CMD_START)
async def cmd_start(message: Message) -> None:
    user = message.from_user
    now = time.monotonic()
    last = _last_start_time.get(user.id, 0.0)
    if now - last < _START_COOLDOWN:
        return  # Prevent duplicate from webhook re-delivery
    _last_start_time[user.id] = now

    # Detect new user registration
    existing = await Database.get_user_settings(user.id)
    is_new_user = existing is None

    # Ensure user exists in DB
    await SessionManager.get_or_create_session(
        user.id, user.username, user.first_name, user.last_name, user.language_code
    )
    _log_request(user.id, "start")

    # Report new user registration to dashboard
    if is_new_user:
        asyncio.ensure_future(report_message(
            user_id=user.id,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            username=user.username or "",
            language_code=user.language_code or "",
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            text=f"New user registered: @{user.username or user.first_name or user.id}",
        ))

    # In groups, skip onboarding — show the group-oriented welcome
    if _is_group_chat(message):
        await _reply_or_answer(message,
            "Hello! I'm an AI assistant powered by NEAR AI and GPT-5.2.\n\n"
            "I can:\n"
            "• Chat with memory\n"
            "• Search the web\n"
            "• Generate images\n"
            "• Read and analyze files (PDFs, images, code)\n"
            "• Calculate math expressions\n\n"
            "*Group chat:*\n"
            "Just @mention me and ask your question! Reply to my messages to continue in a thread.\n"
            "Available: /ping, /model, /start, /help",
        )
        return

    # In private chat, check onboarding
    done = await Database.is_onboarding_done(user.id)
    if not done:
        await _show_onboarding(message)
    else:
        await _reply_or_answer(message,
            "Hello! I'm an AI assistant powered by NEAR AI and GPT-5.2.\n\n"
            "I can:\n"
            "• Chat with memory\n"
            "• Search the web\n"
            "• Generate images\n"
            "• Read and analyze files (PDFs, images, code)\n"
            "• Calculate math expressions\n\n"
            "*Commands (private chat):*\n"
            "/menu — Open the interactive menu\n"
            "/help — Show all available commands\n"
            "/new — Start a new conversation\n"
            "/search <query> — Search the web\n"
            "/imagine <prompt> — Generate an image\n"
            "/calc <expr> — Calculate math\n"
            "/feedback <msg> — Send feedback\n"
            "/model — View or switch AI model\n\n"
            "*Group chat:*\n"
            "Just @mention me and ask your question! Reply to my messages to continue in a thread.\n"
            "Available: /ping, /model, /start, /help",
        )


# Categorized help text for all commands
_COMMAND_CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "General": [
        ("/menu", "Open the interactive menu with all features"),
        ("/start", "Welcome message and bot introduction"),
        ("/help [command]", "Show this help, or details about a specific command"),
        ("/ping", "Check if the bot is responding (works in groups)"),
        ("/model", "View or switch AI model with tap-to-select (works in groups)"),
        ("/chatid", "Show your Telegram user ID"),
        ("/stats", "View your usage statistics and token consumption"),
        ("/feedback <message>", "Send feedback or suggestions to the bot admin"),
    ],
    "Conversations": [
        ("/new", "Start a new conversation (clears history)"),
        ("/conversations", "List, switch, and manage your conversations"),
        ("/switch <id>", "Switch to a specific conversation by ID"),
        ("/rename <id> <title>", "Rename a conversation"),
        ("/delete_conv <id>", "Delete a specific conversation"),
        ("/export", "Export current conversation as a text file"),
        ("/retry", "Regenerate the last response"),
    ],
    "Customization": [
        ("/prompt [text]", "View or set your custom system prompt"),
        ("/settings", "View your current settings"),
        ("/find <keyword>", "Search your conversation history"),
    ],
    "Quick Actions": [
        ("/search <query>", "Search the web instantly"),
        ("/imagine <prompt>", "Generate an image from a description"),
        ("/calc <expression>", "Calculate a math expression"),
    ],
    "Privacy": [
        ("/delete_my_data", "Delete all your data from the bot"),
    ],
}


# Detailed help per command
_COMMAND_DETAILS: dict[str, str] = {
    "start": (
        "Start the bot and see the welcome message with available capabilities.\n"
        "Usage: /start"
    ),
    "new": (
        "Start a fresh conversation by clearing all message history.\n"
        "Aliases: /clear, /reset, /n\n"
        "Usage: /new"
    ),
    "search": (
        "Search the web directly without waiting for the AI to decide to search.\n"
        "Results are returned instantly from the web and added to the conversation.\n"
        "Usage: /search <query>\n"
        "Example: /search latest AI news 2025"
    ),
    "imagine": (
        "Generate an image from a text description.\n"
        "The image is sent directly as a photo in the chat.\n"
        "Usage: /imagine <prompt>\n"
        "Example: /imagine a serene mountain lake at sunset"
    ),
    "calc": (
        "Evaluate a mathematical expression.\n"
        "Supports: +, -, *, /, **, sin, cos, tan, sqrt, log, pi, e, and more.\n"
        "Usage: /calc <expression>\n"
        "Example: /calc 2 * pi * 5"
    ),
    "menu": (
        "Open the interactive menu with quick actions, conversations, settings,\n"
        "and admin panel (for admins). All features accessible through tappable buttons.\n"
        "Alias: /m\n"
        "Usage: /menu"
    ),
    "ping": (
        "Check if the bot is alive and responding.\n"
        "Usage: /ping"
    ),
    "model": (
        "View the current AI model or switch to a different one.\n"
        "Shows all available models as tappable buttons — tap one to switch instantly.\n"
        "Works in both private chats and groups.\n"
        "Usage: /model"
    ),
    "prompt": (
        "View your current system prompt, or set a new one.\n"
        "The system prompt defines how the AI behaves.\n"
        "Usage: /prompt [your custom instructions]"
    ),
    "settings": (
        "View your current settings including system prompt, max messages, and max tokens.\n"
        "Usage: /settings"
    ),
    "stats": (
        "View your usage statistics including requests today, messages this conversation,\n"
        "images generated, and lifetime token consumption.\n"
        "Usage: /stats"
    ),
    "retry": (
        "Retry the last message to get a new response from the AI.\n"
        "Useful if you didn't like the previous response.\n"
        "Usage: /retry"
    ),
    "chatid": (
        "Show your Telegram user ID. Useful for admin operations.\n"
        "Usage: /chatid"
    ),
    "conversations": (
        "List all your conversations with message counts.\n"
        "Tap on a conversation in the inline keyboard to switch to it.\n"
        "Aliases: /conv, /chats\n"
        "Usage: /conversations"
    ),
    "switch": (
        "Switch to a specific conversation by its ID.\n"
        "Use /conversations to see available IDs.\n"
        "Usage: /switch <conversation_id>"
    ),
    "rename": (
        "Rename a conversation to a custom title for easy identification.\n"
        "Usage: /rename <conversation_id> <new title>"
    ),
    "delete_conv": (
        "Delete a specific conversation and all its messages.\n"
        "This cannot be undone.\n"
        "Usage: /delete_conv <conversation_id>"
    ),
    "export": (
        "Export the current conversation as a downloadable text file.\n"
        "Usage: /export"
    ),
    "feedback": (
        "Send feedback, bug reports, or suggestions directly to the bot admin.\n"
        "Your message is stored and can be reviewed by admins.\n"
        "Usage: /feedback <your message>"
    ),
    "find": (
        "Search your conversation history for matching messages.\n"
        "Returns up to 10 matching results with conversation context.\n"
        "Aliases: /searchconv\n"
        "Usage: /find <keyword>\n"
        "Example: /find python"
    ),
    "delete_my_data": (
        "Permanently delete all your data from the bot, including all\n"
        "conversations, messages, usage stats, and feedback.\n"
        "This action cannot be undone.\n"
        "Usage: /delete_my_data"
    ),
    "admin_stats": (
        "View bot-wide statistics including total users, active users,\n"
        "requests today, and uptime. Admin only.\n"
        "Usage: /admin_stats"
    ),
    "admin_users": (
        "Show a list of users currently being processed. Admin only.\n"
        "Usage: /admin_users"
    ),
    "admin_broadcast": (
        "Send a broadcast message to all bot users. Admin only.\n"
        "Usage: /admin_broadcast <message>"
    ),
    "admin_block": (
        "Block a user by their Telegram user ID. Admin only.\n"
        "Usage: /admin_block <user_id>"
    ),
    "admin_unblock": (
        "Unblock a previously blocked user. Admin only.\n"
        "Usage: /admin_unblock <user_id>"
    ),
    "admin_feedback": (
        "View all user-submitted feedback messages. Admin only.\n"
        "Usage: /admin_feedback"
    ),
    "admin_clear_feedback": (
        "Delete all user feedback entries from the database. Admin only.\n"
        "Usage: /admin_clear_feedback"
    ),
}


def _build_help_text(command: str = "", user_id: int = 0) -> str:
    """Build categorized help text for all commands or details for a specific command."""
    if command:
        details = _COMMAND_DETAILS.get(command.lower())
        if details:
            return f"*Help: /{command.lower()}*\n\n{details}"
        # Check aliases
        alias_map: dict[str, str] = {}
        for cat_cmds in _COMMAND_CATEGORIES.values():
            for cmd, _ in cat_cmds:
                name = cmd.split()[0].lstrip("/").lower()
                alias_map[name] = name
                if "," in cmd:
                    aliases = [a.strip().lstrip("/").lower() for a in cmd.split(",")]
                    for a in aliases:
                        alias_map[a] = name
        resolved = alias_map.get(command.lower())
        if resolved and resolved != command.lower():
            return _build_help_text(resolved, user_id)
        return f"No help available for '{command}'. Use /help to see all commands."

    lines = ["*🤖 Bud AI Bot — Commands*", "", "I'm an AI assistant powered by NEAR AI and GPT-5.2.", ""]
    for category, cmds in _COMMAND_CATEGORIES.items():
        lines.append(f"*{category}*")
        for cmd, desc in cmds:
            lines.append(f"  • `{cmd}` — {desc}")
        lines.append("")

    # Add admin commands section for admins
    if user_id and _is_admin(user_id):
        lines.append("*Admin Commands*")
        admin_cmds = [
            ("/admin_stats", "View bot-wide statistics"),
            ("/admin_users", "Show currently active users"),
            ("/admin_broadcast <msg>", "Send a message to all users"),
            ("/admin_block <id>", "Block a user by ID"),
            ("/admin_unblock <id>", "Unblock a user by ID"),
            ("/admin_feedback", "View user feedback messages"),
            ("/admin_clear_feedback", "Clear all feedback"),
            ("/admin_diag", "Real-time system diagnostics dashboard"),
            ("/admin_viewconv <id>", "Inspect a user's conversations & profile"),
            ("/admin_llm_config", "View/set LLM runtime configuration"),
            ("/admin_analytics [days]", "Usage analytics with CSV export"),
            ("/admin_skills", "View registered tool/skill registry"),
        ]
        for cmd, desc in admin_cmds:
            lines.append(f"  • `{cmd}` — {desc}")
        lines.append("")
        lines.append("*Admin Menu Features (tap /menu → Admin Panel)*")
        lines.append("  • 🌐 *Website* — Set a website link shown to all users")
        lines.append("  • 👥 *Group* — Set a group invite link shown to all users")
        lines.append("  • 🔒 *Set Passcode* — Lock a group with a passcode for new joiners")
        lines.append("  • 🔓 *Remove Passcode* — Remove passcode from a group")
        lines.append("")

    lines.extend([
        "_Group chat:_ @mention me to start. Reply to my messages to continue in a thread.",
        "_Group commands:_ /ping, /model",
        "",
        "Use `/help <command>` for detailed help on any command.",
    ])
    return "\n".join(lines)


@router.message(CMD_HELP)
async def cmd_help(message: Message) -> None:
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        command_name = parts[1].strip().lstrip("/")
        help_text = _build_help_text(command_name, user_id)
    else:
        help_text = _build_help_text(user_id=user_id)
    await _reply_or_answer(message,help_text, parse_mode="Markdown")


@router.message(CMD_NEW)
async def cmd_new(message: Message) -> None:
    user_id = message.from_user.id
    await SessionManager.clear_conversation(user_id)
    bust_cache(user_id)
    _log_request(user_id, "new_conversation")
    await _reply_or_answer(message,"Started a new conversation. Previous context has been cleared.")


@router.message(CMD_PROMPT)
async def cmd_prompt(message: Message) -> None:
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        current = await SessionManager.get_system_prompt(user_id)
        await _reply_or_answer(message,
            f"Your current system prompt:\n\n{current}\n\n"
            "To change it: /prompt <your new prompt>"
        )
        return

    new_prompt = parts[1].strip()

    # Prompt injection protection
    safe, reason = _is_safe_prompt(new_prompt)
    if not safe:
        await _reply_or_answer(message,f"Prompt rejected: {reason}")
        return

    await SessionManager.update_system_prompt(user_id, new_prompt)
    _log_request(user_id, "update_prompt")
    await _reply_or_answer(message,"Your custom system prompt has been updated.")


@router.message(CMD_SETTINGS)
async def cmd_settings(message: Message) -> None:
    user_id = message.from_user.id
    user = await Database.get_user_settings(user_id)
    if not user:
        await _reply_or_answer(message,"No settings found. Send /start first.")
        return

    lines = [
        f"*System Prompt:* `{_escape_md(user.get('system_prompt', 'Default'))[:100]}`...",
        f"*Max Messages:* {user.get('max_context_messages', 50)}",
        f"*Max Tokens:* {user.get('max_context_tokens', 128000)}",
    ]
    await _reply_or_answer(message,"\n".join(lines), parse_mode="Markdown")


@router.message(CMD_STATS)
async def cmd_stats(message: Message) -> None:
    user_id = message.from_user.id
    uptime_hours = int(time.time() - _bot_start_time) // 3600 if _bot_start_time else 0

    # Persistent stats from DB
    today = await Database.get_user_usage_today(user_id)
    lifetime = await Database.get_user_lifetime_usage(user_id)

    conv = await Database.get_active_conversation(user_id)
    conv_id = conv["id"] if conv else 0
    all_msgs = await Database.get_conversation_messages(conv_id, limit=None) if conv else []

    lines = [
        "*Your Stats:*",
        f"• Requests today: {today.get('requests', 0)}",
        f"• Messages this conversation: {len(all_msgs)}",
        f"• Images generated: {today.get('images_generated', 0)}",
        f"• Bot uptime: {uptime_hours}h",
    ]
    if lifetime.get("total_requests"):
        lines.append(f"• All-time requests: {lifetime['total_requests']}")
    if lifetime.get("total_tokens_in"):
        lines.append(f"• All-time tokens in: ~{lifetime['total_tokens_in']:,}")
    if lifetime.get("total_tokens_out"):
        lines.append(f"• All-time tokens out: ~{lifetime['total_tokens_out']:,}")

    await _reply_or_answer(message,"\n".join(lines), parse_mode="Markdown")


@router.message(CMD_RETRY)
async def cmd_retry(message: Message) -> None:
    """Resend the last user message to the LLM for a new response."""
    user_id = message.from_user.id
    last_text = _last_user_message.get(user_id)
    if not last_text:
        await _reply_or_answer(message,"No previous message to retry. Send a new message first.")
        return
    _log_request(user_id, "retry")
    # Process the same text again through the normal handler
    if user_id in _processing:
        await _reply_or_answer(message,"Please wait for the current request to finish.")
        return
    _processing.add(user_id)
    try:
        start = time.monotonic()
        await _process_text_response(message, last_text)
        pending = pop_pending_images(user_id)
        for img_path in pending:
            try:
                with open(img_path, "rb") as f:
                    await _reply_or_answer_photo(
                        message,
                        BufferedInputFile(f.read(), filename="image.png"),
                        caption="Generated image",
                    )
                os.remove(img_path)
            except Exception as e:
                logger.error("Failed to send generated image: %s", e)
        elapsed = (time.monotonic() - start) * 1000
        _log_request(user_id, "retry_ok", ms=f"{elapsed:.0f}")
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        logger.error("[user=%d] Retry error after %.0fms: %s", user_id, elapsed, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="retry_error",
            details=f"Retry failed after {elapsed:.0f}ms: {e}",
        ))
        await _reply_or_answer(message,"Sorry, an internal error occurred. Please try again.")
    finally:
        _processing.discard(user_id)


@router.message(CMD_CHATID)
async def cmd_chatid(message: Message) -> None:
    await _reply_or_answer(message,f"Your Telegram user ID: `{message.from_user.id}`", parse_mode="Markdown")


# ─── Quick Action Commands ───

@router.message(CMD_SEARCH)
async def cmd_search(message: Message) -> None:
    """Search the web directly via /search <query>."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,"Usage: /search <query>\nExample: `/search latest technology news`", parse_mode="Markdown")
        return

    query = parts[1].strip()
    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        await _reply_or_answer(message,"Your account has been blocked. Contact an admin.")
        return
    if user_id in _processing:
        await _reply_or_answer(message,"Please wait for the current request to finish.")
        return
    if await _check_rate_limit(user_id, message):
        return

    _log_request(user_id, "quick_search", query=query[:50])
    _processing.add(user_id)
    try:
        # Send a single status message, then edit it with results to avoid
        # sending two separate replies (which users perceive as "double reply").
        status_msg = await message.bot.send_message(
            message.chat.id, f"🔍 Searching for: {query}",
        )
        result = await execute_tool("web_search", {"query": query})
        # Display results — try Markdown first (for the header bold), fallback to plain text
        # NOTE: web_search returns **title** which is NOT Telegram Markdown, and URLs may
        # contain underscores. Telegram legacy Markdown has no escape mechanism, so we
        # must fallback gracefully instead of using broken escape sequences.
        header = "🔍 *Search Results*\n\n"
        max_body = 4000 - len(header)
        body = result
        if len(body) > max_body:
            body = body[:max_body] + "\n\n… (truncated)"
        try:
            await status_msg.edit_text(f"{header}{body}", parse_mode="Markdown")
        except Exception:
            # Markdown entity parsing failed — retry as plain text
            plain = f"🔍 Search Results:\n\n{result}"
            if len(plain) > 4096:
                plain = plain[:4060] + "\n\n… (truncated)"
            await status_msg.edit_text(plain)
        await Database.record_usage(user_id, searches=1)
    except Exception as e:
        logger.error("[user=%d] Quick search error: %s", user_id, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="search_error",
            details=f"Quick search failed: {e}",
        ))
        await _reply_or_answer(message,"Search failed. Please try again later.")
    finally:
        _processing.discard(user_id)


@router.message(CMD_IMAGINE)
async def cmd_imagine(message: Message) -> None:
    """Generate an image directly via /imagine <prompt>."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,"Usage: /imagine <prompt>\nExample: `/imagine a serene mountain lake at sunset`", parse_mode="Markdown")
        return

    prompt = parts[1].strip()
    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        await _reply_or_answer(message,"Your account has been blocked. Contact an admin.")
        return
    if user_id in _processing:
        await _reply_or_answer(message,"Please wait for the current request to finish.")
        return
    if await _check_rate_limit(user_id, message):
        return

    _log_request(user_id, "quick_imagine", prompt=prompt[:50])
    _processing.add(user_id)
    try:
        await _reply_or_answer(message,f"Generating image: {prompt}")

        # Set context var so image_gen knows the user
        image_ctx_var.set(user_id)

        result = await execute_tool("generate_image", {"prompt": prompt})
        await _reply_or_answer(message,result)

        # Send generated image
        pending = pop_pending_images(user_id)
        for img_path in pending:
            try:
                with open(img_path, "rb") as f:
                    await _reply_or_answer_photo(
                        message,
                        BufferedInputFile(f.read(), filename="image.png"),
                        caption="Generated image",
                    )
                os.remove(img_path)
            except Exception as e:
                logger.error("Failed to send generated image: %s", e)
        await Database.record_usage(user_id, images=1)
    except Exception as e:
        logger.error("[user=%d] Quick image error: %s", user_id, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="image_gen_error",
            details=f"Quick image generation failed: {e}",
        ))
        await _reply_or_answer(message,"Image generation failed. Please try again later.")
    finally:
        _processing.discard(user_id)


@router.message(CMD_CALC)
async def cmd_calc(message: Message) -> None:
    """Calculate a math expression directly via /calc <expression>."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,
            "Usage: /calc <expression>\n"
            "Example: `/calc 2 * pi * 5`\n"
            "Supports: +, -, *, /, **, sin, cos, tan, sqrt, log, pi, e",
            parse_mode="Markdown",
        )
        return

    expression = parts[1].strip()
    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        await _reply_or_answer(message,"Your account has been blocked. Contact an admin.")
        return
    if user_id in _processing:
        await _reply_or_answer(message,"Please wait for the current request to finish.")
        return
    if await _check_rate_limit(user_id, message):
        return

    _log_request(user_id, "quick_calc", expr=expression[:50])
    _processing.add(user_id)
    try:
        result = await execute_tool("calculate", {"expression": expression})
        await _reply_or_answer(message,f"*Result:* `{result}`", parse_mode="Markdown")
    except Exception as e:
        logger.error("[user=%d] Quick calc error: %s", user_id, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="calc_error",
            details=f"Quick calc failed: {e}",
        ))
        await _reply_or_answer(message,"Calculation failed. Please try again.")
    finally:
        _processing.discard(user_id)


@router.message(CMD_PING)
async def cmd_ping(message: Message) -> None:
    """Health check — bot replies immediately."""
    db_ok = await Database.ping()
    status = "🟢 All systems operational" if db_ok else "🟡 Bot running (DB connection issue)"
    await _reply_or_answer(message,f"pong! {status}")


@router.message(CMD_MODEL)
async def cmd_model(message: Message) -> None:
    """View or change the AI model. Tap a model button to switch."""
    user_id = message.from_user.id

    current_model = await Database.get_preferred_model(user_id) or settings.near_ai_model

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    for model_name in settings.available_models:
        marker = "● " if model_name == current_model else ""
        builder.button(
            text=f"{marker}{model_name}",
            callback_data=f"model_select:{model_name}",
        )
    builder.adjust(1)

    models_list = "\n".join(f"• `{m}`" for m in settings.available_models)
    text = (
        f"*Current Model:* `{current_model}`\n\n"
        f"*Available Models:*\n{models_list}\n\n"
        f"_Tap a model above to switch instantly._"
    )
    await _reply_or_answer(message, text, parse_mode="Markdown", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("model_select:"))
async def callback_model_select(callback: CallbackQuery) -> None:
    """Handle model selection from inline keyboard."""
    user_id = callback.from_user.id
    model_name = callback.data.split(":", 1)[1]

    if model_name not in settings.available_models:
        await callback.answer("Invalid model.", show_alert=True)
        return

    # Capture old model before switching, for dashboard reporting
    old_model = await Database.get_preferred_model(user_id) or settings.near_ai_model
    await Database.set_preferred_model(user_id, model_name)
    await callback.answer(f"Switched to {model_name}")

    # Report model switch to dashboard
    asyncio.ensure_future(report_event(
        "model_switch",
        user_id=user_id,
        message_text=f"Switched to {model_name}",
        msg_type="settings",
        extra={"previous_model": old_model, "new_model": model_name},
    ))

    # Refresh the model selection message
    current_model = await Database.get_preferred_model(user_id) or settings.near_ai_model
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    for m in settings.available_models:
        marker = "● " if m == current_model else ""
        builder.button(text=f"{marker}{m}", callback_data=f"model_select:{m}")
    builder.adjust(1)

    models_list = "\n".join(f"• `{m}`" for m in settings.available_models)
    text = (
        f"*Current Model:* `{current_model}`\n\n"
        f"*Available Models:*\n{models_list}\n\n"
        f"_Tap a model above to switch instantly._"
    )
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        pass
    _log_request(user_id, "model_change", model=model_name)


@router.message(CMD_FEEDBACK)
async def cmd_feedback(message: Message) -> None:
    """Submit feedback to the bot admin."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,
            "Send your feedback, suggestions, or bug reports:\n\n"
            "Usage: `/feedback <your message>`\n"
            "Example: `/feedback It would be great if you could add a weather command!`",
            parse_mode="Markdown",
        )
        return

    feedback_text = parts[1].strip()
    await Database.save_feedback(user_id, feedback_text)
    _log_request(user_id, "feedback", text=feedback_text[:100])

    # Notify all admins about new feedback
    admin_notified = False
    for admin_id in settings.admin_ids:
        try:
            user_info = message.from_user
            name = user_info.full_name or user_info.username or str(user_id)
            safe_name = _escape_md(name)
            safe_feedback = _escape_md(feedback_text)
            await message.bot.send_message(
                admin_id,
                f"*New Feedback from {safe_name} (ID: {user_id})*\n\n{safe_feedback}",
                parse_mode="Markdown",
            )
            admin_notified = True
        except Exception:
            pass

    if admin_notified:
        await _reply_or_answer(message,"Thank you for your feedback! It has been sent to the bot admin.")
    else:
        await _reply_or_answer(message,"Thank you for your feedback! It has been recorded.")


# ─── Find Command ───

@router.message(CMD_FIND)
async def cmd_find(message: Message) -> None:
    """Search the user's conversation history for a keyword."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,
            "Usage: /find <keyword>\n"
            "Example: `/find python`\n\n"
            "Searches all your conversations for matching messages.",
            parse_mode="Markdown",
        )
        return

    keyword = parts[1].strip()
    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        await _reply_or_answer(message,"Your account has been blocked.")
        return

    _log_request(user_id, "find", keyword=keyword[:50])
    results = await Database.search_user_messages(user_id, keyword, limit=10)

    if not results:
        await _reply_or_answer(message,
            f"No matches found for `{_escape_md(keyword)}` in your conversations.",
            parse_mode="Markdown",
        )
        return

    lines = [f"*Search results for:* `{_escape_md(keyword)}`", ""]
    for r in results:
        role = r["role"].upper()
        content = r["content"][:120]
        conv_title = r.get("conv_title", "Untitled")[:30]
        ts = r["created_at"].strftime("%m-%d %H:%M") if r.get("created_at") else ""
        safe_title = _escape_md(conv_title)
        safe_content = _escape_md(content)
        lines.append(f"💬 *{safe_title}* — {role} [{ts}]")
        lines.append(f"  `{safe_content}{'...' if len(r['content']) > 120 else ''}`")
        lines.append("")

    await _reply_or_answer(message, "\n".join(lines), parse_mode="Markdown")


# ─── Delete My Data ───

@router.message(CMD_DELETE_MY_DATA)
async def cmd_delete_my_data(message: Message) -> None:
    """Permanently delete all user data."""
    user_id = message.from_user.id

    # In groups, don't allow this
    if _is_group_chat(message):
        await _reply_or_answer(message,
            "Please run this command in a private chat with me."
        )
        return

    text = message.text or ""
    parts = text.split(maxsplit=1)
    confirmation = parts[1].strip().lower() if len(parts) > 1 else ""

    if confirmation != "yes":
        await _reply_or_answer(message,
            "⚠️ *Delete All My Data*\n\n"
            "This will permanently delete all your conversations, messages, "
            "usage stats, and feedback. This cannot be undone.\n\n"
            "To confirm, send:\n"
            "`/delete_my_data yes`",
            parse_mode="Markdown",
        )
        return

    _log_request(user_id, "delete_my_data")
    deleted = await Database.delete_user_data(user_id)
    # Clean up in-memory state
    _pending_menu_action.pop(user_id, None)
    _last_user_message.pop(user_id, None)
    _processing.discard(user_id)
    bust_cache(user_id)
    logger.info("[user=%d] Deleted all data (%d conversations)", user_id, deleted)

    await _reply_or_answer(message,
        "✅ All your data has been deleted. Your conversations, messages, "
        "and usage history are gone.\n\n"
        "Send /start to use the bot again."
    )


# ─── Admin Feedback Commands ───

@router.message(CMD_ADMIN_FEEDBACK)
async def cmd_admin_feedback(message: Message) -> None:
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return

    all_feedback = await Database.get_all_feedback(limit=20)
    if not all_feedback:
        await _reply_or_answer(message,"No feedback recorded yet.")
        return

    lines = ["*User Feedback:*", ""]
    for fb in all_feedback:
        name = fb.get("first_name") or fb.get("username") or str(fb["user_id"])
        ts = fb["created_at"].strftime("%Y-%m-%d %H:%M") if fb.get("created_at") else "?"
        msg_text = fb["message"][:200]
        safe_name = _escape_md(name)
        safe_msg = _escape_md(msg_text)
        lines.append(f"• *{safe_name}* (ID: {fb['user_id']}) [{ts}]:")
        lines.append(f"  _{safe_msg}_")
        lines.append("")

    for chunk in _chunk_lines(lines, 15):
        await _reply_or_answer(message,"\n".join(chunk), parse_mode="Markdown")


@router.message(CMD_ADMIN_CLEAR_FEEDBACK)
async def cmd_admin_clear_feedback(message: Message) -> None:
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return
    deleted = await Database.clear_feedback()
    await _reply_or_answer(message,f"Deleted {deleted} feedback entries.")
    asyncio.ensure_future(report_admin_action(
        admin_id=user_id, action="clear_feedback",
        details=f"Deleted {deleted} feedback entries",
    ))


# ─── Conversation Management ───

@router.message(CMD_CONVERSATIONS)
async def cmd_conversations(message: Message) -> None:
    """List all conversations with inline keyboard for management."""
    user_id = message.from_user.id
    convs = await Database.list_conversations(user_id)

    if not convs:
        await _reply_or_answer(message,"No conversations yet. Send a message to start one!")
        return

    # Build inline keyboard
    builder = InlineKeyboardBuilder()
    for conv in convs:
        title = conv.get("title", "Untitled")[:30]
        msg_count = conv.get("msg_count", 0)
        label = f"{title} ({msg_count} msgs)"
        builder.button(text=label, callback_data=f"conv_switch:{conv['id']}")

    builder.adjust(1)  # One button per row
    kb = builder.as_markup()

    await _reply_or_answer(message,
        "Your conversations (tap to switch):\n\n"
        "• /switch <id> — Switch to a conversation\n"
        "• /rename <id> <title> — Rename a conversation\n"
        "• /delete_conv <id> — Delete a conversation\n"
        "• /export — Export the current conversation",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("conv_switch"))
async def callback_switch_conversation(callback: CallbackQuery) -> None:
    """Handle inline keyboard conversation switch."""
    conv_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    ok = await Database.set_active_conversation(user_id, conv_id)
    if ok:
        conv = await Database.get_conversation_by_id(conv_id)
        title = conv.get("title", "Conversation") if conv else "Conversation"
        await callback.message.edit_text(f"Switched to: *{_escape_md(title)}*", parse_mode="Markdown")
    else:
        await callback.message.edit_text("Failed to switch conversation.")
    await callback.answer()


@router.message(CMD_SWITCH)
async def cmd_switch(message: Message) -> None:
    """Switch to a specific conversation by ID."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,"Usage: /switch <conversation_id>")
        return

    try:
        conv_id = int(parts[1].strip())
    except ValueError:
        await _reply_or_answer(message,"Invalid conversation ID. Use /conversations to see IDs.")
        return

    ok = await Database.set_active_conversation(user_id, conv_id)
    if ok:
        conv = await Database.get_conversation_by_id(conv_id)
        title = conv.get("title", "Conversation") if conv else "Conversation"
        await _reply_or_answer(message,f"Switched to: *{_escape_md(title)}*", parse_mode="Markdown")
    else:
        await _reply_or_answer(message,"Conversation not found or not yours.")


@router.message(CMD_RENAME)
async def cmd_rename(message: Message) -> None:
    """Rename a conversation."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await _reply_or_answer(message,"Usage: /rename <conversation_id> <new title>")
        return

    try:
        conv_id = int(parts[1].strip())
    except ValueError:
        await _reply_or_answer(message,"Invalid conversation ID.")
        return

    new_title = parts[2].strip()
    if not new_title:
        await _reply_or_answer(message,"Title cannot be empty.")
        return

    conv = await Database.get_conversation_by_id(conv_id)
    if not conv or conv["user_id"] != user_id:
        await _reply_or_answer(message,"Conversation not found or not yours.")
        return

    await Database.update_conversation_title(conv_id, new_title)
    await _reply_or_answer(message,f"Conversation renamed to: *{_escape_md(new_title)}*", parse_mode="Markdown")


@router.message(CMD_DELETE_CONV)
async def cmd_delete_conv(message: Message) -> None:
    """Delete a specific conversation."""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,"Usage: /delete_conv <conversation_id>")
        return

    try:
        conv_id = int(parts[1].strip())
    except ValueError:
        await _reply_or_answer(message,"Invalid conversation ID.")
        return

    conv = await Database.get_conversation_by_id(conv_id)
    if not conv or conv["user_id"] != user_id:
        await _reply_or_answer(message,"Conversation not found or not yours.")
        return

    await Database.delete_conversation(conv_id)
    await _reply_or_answer(message,"Conversation deleted.")


@router.message(CMD_EXPORT)
async def cmd_export(message: Message) -> None:
    """Export the current conversation as a text file."""
    user_id = message.from_user.id
    conv = await Database.get_active_conversation(user_id)

    if not conv:
        await _reply_or_answer(message,"No active conversation to export.")
        return

    export_text = await Database.export_conversation(conv["id"])
    if not export_text:
        await _reply_or_answer(message,"No messages in the current conversation.")
        return

    try:
        export_bytes = export_text.encode("utf-8")
        await message.answer_document(
            BufferedInputFile(export_bytes, filename=f"conversation_{conv['id']}.txt"),
            caption=f"Exported conversation: {conv.get('title', 'Untitled')}",
            parse_mode=None,
        )
    except Exception as e:
        logger.error("Export failed: %s", e)
        await _reply_or_answer(message,"Failed to export conversation.")


# ─── Admin Commands ───

@router.message(CMD_ADMIN_STATS)
async def cmd_admin_stats(message: Message) -> None:
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return

    try:
        total_users = await Database.get_total_user_count()
    except Exception:
        total_users = len(_processing)

    active_now = len(_processing)
    uptime_hours = int(time.time() - _bot_start_time) // 3600

    try:
        today_usage = await Database.get_all_users_usage_today()
        total_today_requests = sum(u.get("requests", 0) for u in today_usage)
    except Exception:
        total_today_requests = 0

    lines = [
        "*Bot Admin Stats:*",
        f"• Total users: {total_users}",
        f"• Active now: {active_now}",
        f"• Requests today: {total_today_requests}",
        f"• Uptime: {uptime_hours}h",
    ]
    await _reply_or_answer(message,"\n".join(lines), parse_mode="Markdown")


@router.message(CMD_ADMIN_USERS)
async def cmd_admin_users(message: Message) -> None:
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return

    active = list(_processing)
    if active:
        lines = ["*Currently processing:*"] + [f"• `{uid}`" for uid in active[:20]]
        await _reply_or_answer(message,"\n".join(lines), parse_mode="Markdown")
    else:
        await _reply_or_answer(message,"No users currently processing.")


@router.message(CMD_ADMIN_BROADCAST)
async def cmd_admin_broadcast(message: Message) -> None:
    """Send a broadcast message to all users. Fan-out with per-user error handling."""
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return

    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,"Usage: /admin_broadcast <message>")
        return

    broadcast_text = parts[1].strip()
    _log_request(user_id, "admin_broadcast")

    # Acknowledge to admin
    await _reply_or_answer(message,f"Broadcasting message to all users...\n\n{broadcast_text[:100]}")

    try:
        all_users = await Database.get_all_user_ids()
    except Exception as e:
        await _reply_or_answer(message,f"Failed to fetch users: {e}")
        return

    if not all_users:
        await _reply_or_answer(message,"No users to broadcast to.")
        return

    sent = 0
    failed = 0
    for uid in all_users:
        try:
            await message.bot.send_message(
                uid,
                f"*📢 Broadcast from admin:*\n\n{_escape_md(broadcast_text)}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning("Broadcast failed for user %d: %s", uid, e)
        # Small delay to avoid hitting Telegram rate limits
        if sent % 10 == 0:
            await asyncio.sleep(0.5)

    asyncio.ensure_future(report_admin_action(
        admin_id=user_id, action="broadcast",
        details=f"Broadcast to {len(all_users)} users (sent: {sent}, failed: {failed})",
        extra={"total_users": len(all_users), "sent": sent, "failed": failed},
    ))
    await _reply_or_answer(message,f"Broadcast complete. Sent: {sent}, Failed: {failed}")


@router.message(CMD_ADMIN_BLOCK)
async def cmd_admin_block(message: Message) -> None:
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,"Usage: /admin_block <user_id>")
        return
    try:
        target_id = int(parts[1].strip())
        await Database.set_user_blocked(target_id, True)
        await _reply_or_answer(message,f"User {target_id} has been blocked.")
        asyncio.ensure_future(report_admin_action(
            admin_id=user_id, action="block_user",
            details=f"Blocked user {target_id}",
            target_user_id=target_id,
        ))
    except ValueError:
        await _reply_or_answer(message,"Invalid user ID.")


@router.message(CMD_ADMIN_UNBLOCK)
async def cmd_admin_unblock(message: Message) -> None:
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(message,"Usage: /admin_unblock <user_id>")
        return
    try:
        target_id = int(parts[1].strip())
        await Database.set_user_blocked(target_id, False)
        await _reply_or_answer(message,f"User {target_id} has been unblocked.")
        asyncio.ensure_future(report_admin_action(
            admin_id=user_id, action="unblock_user",
            details=f"Unblocked user {target_id}",
            target_user_id=target_id,
        ))
    except ValueError:
        await _reply_or_answer(message,"Invalid user ID.")


# ═══════════════════════════════════════════════════════════════
#  Premium Admin Commands
# ═══════════════════════════════════════════════════════════════


@router.message(CMD_ADMIN_VIEWCONV)
async def cmd_admin_viewconv(message: Message) -> None:
    """Inspect a user's conversations, messages, and settings."""
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return

    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_or_answer(
            message,
            "Usage: `/admin_viewconv <user_id>`\n\n"
            "Shows user profile, conversation list, and per-conversation message counts.",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(parts[1].strip())
    except ValueError:
        await _reply_or_answer(message,"❌ Invalid user ID. Must be a number.")
        return

    profile = await Database.get_user_full_profile(target_id)
    if not profile:
        await _reply_or_answer(message,f"❌ User `{target_id}` not found.", parse_mode="Markdown")
        return

    username = profile.get("username") or "—"
    first_name = profile.get("first_name") or "—"
    blocked = "🚫 Blocked" if profile.get("is_blocked") else "✅ Active"
    model = profile.get("preferred_model") or "default"
    sys_prompt = profile.get("system_prompt", "")
    if len(sys_prompt) > 100:
        sys_prompt = sys_prompt[:100] + "..."

    lines = [
        f"*User Profile: `{target_id}`*",
        f"👤 Name: {_escape_md(first_name)} (@{_escape_md(username)})",
        f"📊 Status: {blocked} | Model: `{model}`",
        f"💬 Conversations: {profile.get('conversation_count', 0)}",
        f"📝 Messages: {profile.get('total_messages', 0)}",
        f"🔢 Requests: {profile.get('total_req', 0)}",
        f"📥 Tokens in: {profile.get('total_in', 0):,}",
        f"📤 Tokens out: {profile.get('total_out', 0):,}",
        f"🖼️ Images: {profile.get('total_img', 0)}",
        f"⚙️ System prompt: `{_escape_md(sys_prompt)}`",
    ]

    # Get conversation list
    convs = await Database.get_user_conversations_with_messages(target_id)
    if convs:
        lines.append("")
        lines.append("*Recent Conversations:*")
        for i, conv in enumerate(convs[:5], 1):
            title = conv.get("title", "Untitled")
            msgs = conv.get("msg_count", 0)
            last = conv.get("last_msg", "")
            if last and len(last) > 60:
                last = last[:60] + "..."
            lines.append(f"  `{conv['id']}` — *{_escape_md(title)}* ({msgs} msgs)")
            if last:
                lines.append(f"       ↳ {_escape_md(last)}")
    else:
        lines.append("")
        lines.append("No conversations found.")

    await _reply_or_answer(message, "\n".join(lines), parse_mode="Markdown")


@router.message(CMD_ADMIN_LLM_CONFIG)
async def cmd_admin_llm_config(message: Message) -> None:
    """View and modify live LLM configuration settings."""
    user_id = message.from_user.id
    if not _is_admin(user_id):
        await _reply_or_answer(message, "⛔ Access denied. Only bot admins can use this command.")
        return

    text = message.text or ""
    parts = text.split(maxsplit=3)

    # If setting a value: /admin_llm_config set <key> <value>
    if len(parts) >= 4 and parts[1].lower() == "set":
        key = parts[2].strip()
        value = parts[3].strip()
        # Validate known keys
        valid_keys = {"temperature", "max_tokens", "model", "image_model", "image_size"}
        if key not in valid_keys:
            await _reply_or_answer(
                message,
                f"❌ Unknown config key `{key}`.\nValid keys: {', '.join(f'`{k}`' for k in sorted(valid_keys))}",
                parse_mode="Markdown",
            )
            return
        await Database.set_config(f"llm_{key}", value)
        await _reply_or_answer(message,f"✅ LLM config `{key}` set to `{value}`", parse_mode="Markdown")
        asyncio.ensure_future(report_admin_action(
            admin_id=user_id, action="llm_config_set",
            details=f"Set LLM config {key}={value}",
        ))
        return

    # If resetting: /admin_llm_config reset <key>
    if len(parts) >= 3 and parts[1].lower() == "reset":
        key = parts[2].strip()
        await Database.delete_config(f"llm_{key}")
        await _reply_or_answer(message,f"✅ LLM config `{key}` reset to default.", parse_mode="Markdown")
        asyncio.ensure_future(report_admin_action(
            admin_id=user_id, action="llm_config_reset",
            details=f"Reset LLM config {key} to default",
        ))
        return

    # Show current configuration
    overrides = {}
    for k in ("temperature", "max_tokens", "model", "image_model", "image_size"):
        val = await Database.get_config(f"llm_{k}")
        if val:
            overrides[k] = val

    lines = [
        "*LLM Configuration*",
        "",
        "*Default (Environment):*",
        f"  • Model: `{settings.near_ai_model}`",
        f"  • Image Model: `{settings.near_ai_image_model}`",
        f"  • Image Size: `{settings.near_ai_image_size}`",
        f"  • Max Tokens: `{settings.max_response_tokens}`",
        f"  • Base URL: `{settings.near_ai_base_url}`",
    ]

    if overrides:
        lines.extend(["", "*Runtime Overrides (bot_config):*"])
        for k, v in overrides.items():
            lines.append(f"  • `{k}` = `{v}`")
        lines.append("")
        lines.append("Use `/admin_llm_config reset <key>` to clear an override.")
    else:
        lines.extend(["", "No runtime overrides set."])

    lines.extend([
        "",
        "*Commands:*",
        "  `/admin_llm_config` — Show this view",
        "  `/admin_llm_config set temperature 0.7` — Set value",
        "  `/admin_llm_config reset model` — Reset to default",
        "",
        "*Valid keys:* temperature, max_tokens, model, image_model, image_size",
    ])

    await _reply_or_answer(message, "\n".join(lines), parse_mode="Markdown")


@router.message(CMD_ADMIN_DIAG)
async def cmd_admin_diag(message: Message) -> None:
    """Real-time system diagnostics dashboard."""
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return

    uptime_secs = int(time.time() - _bot_start_time) if _bot_start_time else 0
    uptime_h = uptime_secs // 3600
    uptime_m = (uptime_secs % 3600) // 60

    # DB pool
    db_stats = await Database.get_db_pool_stats()

    # System stats
    total_users = await Database.get_total_user_count()
    total_convs = 0
    try:
        pool = await Database.get_pool()
        async with pool.acquire() as conn:
            total_convs = await conn.fetchval("SELECT COUNT(*) FROM conversations")
    except Exception:
        pass

    total_msgs = await Database.get_total_messages_count()

    # In-memory state
    active_processing = len(_processing)
    pending_passcodes = len(_pending_passcode)
    pending_actions = len(_pending_menu_action)
    cache_entries = len(_last_user_message)

    lines = [
        "*🩺 System Diagnostics*",
        "",
        f"*⏱️ Uptime:* {uptime_h}h {uptime_m}m",
        f"*🟡 Degraded:* {'YES (DB fallback)' if Database.is_degraded() else 'No'}",
        f"*💾 DB Pool:* {db_stats.get('status', '?')} | "
        f"{db_stats.get('free', 0)}/{db_stats.get('pool_size', 0)} free "
        f"(min={db_stats.get('min_size', '?')}, max={db_stats.get('max_size', '?')})",
        "",
        "*📊 Database Counters:*",
        f"  • Users: `{total_users}`",
        f"  • Conversations: `{total_convs}`",
        f"  • Messages: `{total_msgs}`",
        "",
        "*🧠 In-Memory State:*",
        f"  • Processing queue: `{active_processing}`",
        f"  • Pending passcodes: `{pending_passcodes}`",
        f"  • Pending menu actions: `{pending_actions}`",
        f"  • Cached user messages: `{cache_entries}`",
        "",
        "*⚙️ Runtime:*",
        f"  • Model: `{settings.near_ai_model}`",
        f"  • Image model: `{settings.near_ai_image_model}`",
        f"  • Rate limit: `{settings.rate_limit_max_requests}` req/{settings.rate_limit_window_seconds}s",
        f"  • Daily cap: `{settings.rate_limit_daily_cap}` req",
        f"  • Webhook: `{settings.use_webhook}`",
    ]

    await _reply_or_answer(message, "\n".join(lines), parse_mode="Markdown")


@router.message(CMD_ADMIN_ANALYTICS)
async def cmd_admin_analytics(message: Message) -> None:
    """Usage analytics with optional day range and CSV export."""
    user_id = message.from_user.id
    if not _is_admin(user_id):
        return

    text = message.text or ""
    parts = text.split(maxsplit=2)

    # Parse optional days parameter
    days = 7
    export_mode = False
    if len(parts) >= 2:
        # Check for "export" keyword first
        if parts[1].strip().lower() == "export":
            export_mode = True
            if len(parts) >= 3:
                try:
                    days = int(parts[2].strip())
                    days = max(1, min(365, days))
                except ValueError:
                    pass  # Use default 7
        else:
            try:
                days = int(parts[1].strip())
                days = max(1, min(365, days))
            except ValueError:
                await _reply_or_answer(message,"Usage: `/admin_analytics [days]` or `/admin_analytics export [days]`")
                return

    analytics = await Database.get_usage_analytics(days)

    # Handle export as CSV
    if export_mode:
        csv_lines = ["date,requests,tokens_in,tokens_out"]
        for day in analytics.get("daily_breakdown", []):
            csv_lines.append(
                f"{day['date']},{day['reqs']},{day['t_in']},{day['t_out']}"
            )
        csv_text = "\n".join(csv_lines)
        csv_bytes = csv_text.encode("utf-8")
        await message.answer_document(
            BufferedInputFile(csv_bytes, filename=f"analytics_{days}d.csv"),
            caption=f"📊 Analytics export ({days} days)",
        )
        return

    lines = [
        f"*📊 Usage Analytics — Last {days} Days*",
        "",
        f"*Overview:*",
        f"  • Total requests: `{analytics['total_requests']:,}`",
        f"  • Active users: `{analytics['active_users']}`",
        f"  • Tokens in: `{analytics['tokens_in']:,}`",
        f"  • Tokens out: `{analytics['tokens_out']:,}`",
        f"  • Images generated: `{analytics['images_generated']}`",
    ]

    # Daily breakdown (compact)
    daily = analytics.get("daily_breakdown", [])
    if daily:
        lines.extend(["", "*Daily Breakdown:*"])
        for d in daily:
            lines.append(
                f"  `{d['date']}` — {d['reqs']} req, {d['t_in']} in, {d['t_out']} out"
            )

    # Top users
    top = analytics.get("top_users", [])
    if top:
        lines.extend(["", "*Top Users:*"])
        for i, u in enumerate(top, 1):
            uname = u.get("username") or f"user_{u['user_id']}"
            safe_uname = _escape_md(uname)
            lines.append(f"  {i}. `{u['user_id']}` (@{safe_uname}) — {u['reqs']} req")

    lines.extend([
        "",
        "📥 Use `/admin_analytics export [days]` for CSV export.",
    ])

    await _reply_or_answer(message, "\n".join(lines), parse_mode="Markdown")


@router.message(CMD_ADMIN_SKILLS)
async def cmd_admin_skills(message: Message) -> None:
    """View registered tool/skill definitions and their status."""
    user_id = message.from_user.id
    if not _is_admin(user_id):
        await _reply_or_answer(message, "⛔ Access denied. Only bot admins can use this command.")
        return

    from app.skills.registry import _TOOL_HANDLERS, ALL_TOOL_DEFINITIONS, execute_tool

    lines = [
        "*🔧 Skill Registry Manager*",
        "",
        f"*Registered Tools:* `{len(_TOOL_HANDLERS)}`",
        "",
    ]

    for tool_def in ALL_TOOL_DEFINITIONS:
        name = tool_def.get("function", {}).get("name", "unknown")
        desc = tool_def.get("function", {}).get("description", "—")
        params = tool_def.get("function", {}).get("parameters", {})
        required = params.get("required", [])
        props = params.get("properties", {})

        # Check if handler is registered
        has_handler = "✅" if name in _TOOL_HANDLERS else "❌"
        lines.append(f"{has_handler} `{name}`")
        lines.append(f"   📝 {desc[:120]}")
        if props:
            lines.append(f"   📥 Params: {', '.join(f'`{k}`' for k in props.keys())}")
        if required:
            lines.append(f"   ⚠️ Required: {', '.join(f'`{k}`' for k in required)}")
        lines.append("")

    handler_names = list(_TOOL_HANDLERS.keys())
    defined_names = [t.get("function", {}).get("name", "") for t in ALL_TOOL_DEFINITIONS]
    orphaned = [n for n in handler_names if n not in defined_names]
    if orphaned:
        lines.append(f"*⚠️ Orphaned handlers (no definition):* {', '.join(orphaned)}")

    await _reply_or_answer(message, "\n".join(lines), parse_mode="Markdown")


# ─────────────────────────── File Handler ───────────────────────────

# Blocked extensions for direct upload
_BLOCKED_UPLOAD_EXTS = {ext.lower() for ext in settings.blocked_file_extensions}


def _is_blocked_filename(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in _BLOCKED_UPLOAD_EXTS


@router.message(F.document | F.photo)
async def handle_file(message: Message) -> None:
    user_id = message.from_user.id

    # Group chat support — only respond when mentioned or replied to
    if not await _should_respond_in_group(message):
        return

    # Check if there's a pending "read_file" menu action — consume it
    was_menu_action = bool(_pending_menu_action.pop(user_id, None))

    # Check if blocked
    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        await _reply_or_answer(message,"Your account has been blocked. Contact an admin.")
        return

    if user_id in _processing:
        await _reply_or_answer(message,"Please wait for the current request to finish.")
        return

    # Lock immediately
    _processing.add(user_id)

    # File type validation at handler level
    if message.document:
        file_name = message.document.file_name or "unknown_file"
        if _is_blocked_filename(file_name):
            _processing.discard(user_id)
            await _reply_or_answer(message,
                f"File type '{os.path.splitext(file_name)[1]}' is not supported for security reasons."
            )
            return

    if await _check_rate_limit(user_id, message):
        _processing.discard(user_id)
        return

    try:
        start = time.monotonic()

        # Check file size before downloading
        if message.document:
            max_bytes = settings.file_max_size_mb * 1024 * 1024
            if message.document.file_size > max_bytes:
                _processing.discard(user_id)
                await _reply_or_answer(message,
                    f"File is too large ({message.document.file_size / 1024 / 1024:.1f} MB). "
                    f"Maximum allowed is {settings.file_max_size_mb} MB."
                )
                return

        await _reply_or_answer(message,"Downloading file...")

        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name or "unknown_file"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_name = "photo.jpg"
        else:
            _processing.discard(user_id)
            await _reply_or_answer(message,"Unsupported file type.")
            return

        file = await message.bot.get_file(file_id)
        if not file.file_path:
            _processing.discard(user_id)
            await _reply_or_answer(message,"Could not access file.")
            return

        os.makedirs("/tmp/bot_downloads", exist_ok=True)
        local_path = os.path.join("/tmp/bot_downloads", f"{user_id}_{int(time.time())}_{file_name}")
        await message.bot.download_file(file.file_path, destination=local_path)
        await _reply_or_answer(message,f"File downloaded: {file_name}. Analyzing...")

        analysis_prompt = message.caption or f"Analyze this file: {file_name}"

        if message.photo:
            # Check vision prompt for injection attempts
            safe, reason = _is_safe_prompt(analysis_prompt)
            if not safe:
                _processing.discard(user_id)
                await _reply_or_answer(message,f"Analysis prompt rejected: {reason}")
                return
            await _process_vision_response(message, local_path, analysis_prompt, user_id)
        else:
            await _process_text_response(
                message,
                f"User uploaded a file '{file_name}'. Analyze it using the read_file tool "
                f"with path='{local_path}' and analysis_prompt='{analysis_prompt}'.",
            )

        # If the file was uploaded via the menu "Read File" action, restore the
        # pinned menu (which was showing the input prompt) so the user sees the
        # menu again instead of a stale prompt.
        if was_menu_action:
            try:
                await _return_to_page(message, user_id)
            except Exception:
                pass

        elapsed = (time.monotonic() - start) * 1000
        _log_request(user_id, "file_done", ms=f"{elapsed:.0f}")
        asyncio.ensure_future(report_message(
            user_id=user_id,
            text=f"File uploaded and processed: {file_name}",
            chat_id=message.chat.id,
            chat_type=message.chat.type,
        ))
    except Exception as e:
        logger.error("[user=%d] File handler error: %s", user_id, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="file_handler_error",
            details=f"File handler error: {e}",
        ))
        await _reply_or_answer(message,"Sorry, an error occurred while processing your file. Please try again.")
    finally:
        _processing.discard(user_id)


# ─────────────────────────── Text Message Handler ───────────────────────────

@router.message(F.text, ~F.text.startswith("/"))
async def handle_message(message: Message) -> None:
    user_id = message.from_user.id

    text = (message.text or "").strip()

    # Check onboarding state — if user hasn't completed onboarding, prompt them
    if not _is_group_chat(message):
        done = await Database.is_onboarding_done(user_id)
        if not done:
            website_url = await Database.get_config("website_url")
            group_link = await Database.get_config("group_link")
            parts = ["Please choose one of the options above to get started:"]
            if group_link:
                parts.append("👥 Tap *Join Telegram Group* to join the community.")
            if website_url:
                parts.append("🌐 Tap *Use Web Version* to use the web app.")
            parts.append("💬 Tap *Continue in DM* to start chatting directly with me.")
            await message.reply("\n".join(parts), parse_mode="Markdown")
            return

    # Check for pending passcode verification (DM only)
    if not _is_group_chat(message) and await _handle_passcode_verification(user_id, text, message):
        return

    # Group chat support — only respond when mentioned, replied to, or command
    if not await _should_respond_in_group(message):
        return

    # Check if blocked
    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        await _reply_or_answer(message,"Your account has been blocked. Contact an admin.")
        return

    if user_id in _processing:
        await _reply_or_answer(message,"Please wait for the current request to finish.")
        return

    # Content-based dedup: if this exact text from this user was just processed,
    # this is a late Telegram webhook retry — silently drop it.
    now_ts = time.time()
    content_key = (user_id, text)
    last_ts = _recent_messages.get(content_key)
    if last_ts and (now_ts - last_ts) < _MESSAGE_TTL:
        logger.debug("Dropping late retry from user %d (same text within %ds)", user_id, _MESSAGE_TTL)
        return

    # Periodic cleanup of stale content dedup entries
    if len(_recent_messages) > 500:
        cutoff = now_ts - _MESSAGE_TTL
        stale = [k for k, ts in _recent_messages.items() if ts < cutoff]
        for k in stale:
            del _recent_messages[k]

    # Lock immediately — prevents parallel processing of the same user
    _processing.add(user_id)

    if await _check_rate_limit(user_id, message):
        _processing.discard(user_id)
        return

    # Check if this text is input for a pending menu action
    if not text:
        _processing.discard(user_id)
        return
    if await process_pending_action(message, user_id, text):
        _processing.discard(user_id)
        return  # Message was consumed by a menu action

    try:
        start = time.monotonic()

        # Store last user message for /retry
        _last_user_message[user_id] = text

        await _process_text_response(message, text)

        # Send any pending generated images as photos
        pending = pop_pending_images(user_id)
        for img_path in pending:
            try:
                with open(img_path, "rb") as f:
                    await _reply_or_answer_photo(
                        message,
                        BufferedInputFile(f.read(), filename="image.png"),
                        caption="Generated image",
                    )
                os.remove(img_path)
            except Exception as e:
                logger.error("Failed to send generated image: %s", e)

        elapsed = (time.monotonic() - start) * 1000
        _log_request(user_id, "response_ok", ms=f"{elapsed:.0f}")
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        logger.error("[user=%d] Handler error after %.0fms: %s", user_id, elapsed, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="handler_error",
            details=f"Message handler failed after {elapsed:.0f}ms: {e}",
        ))
        await _reply_or_answer(message,"Sorry, an internal error occurred. Please try again.")
    finally:
        _processing.discard(user_id)
        # Record processed message text for content-based retry dedup
        _recent_messages[content_key] = time.time()


# ─────────────────────────── Voice Message Handler ───────────────────────────

@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    """Handle voice messages: transcribe via STT then process as text."""
    user_id = message.from_user.id

    # Group chat support — only respond when mentioned or replied to
    if not await _should_respond_in_group(message):
        return

    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        await _reply_or_answer(message, "Your account has been blocked. Contact an admin.")
        return

    if user_id in _processing:
        await _reply_or_answer(message, "Please wait for the current request to finish.")
        return

    if await _check_rate_limit(user_id, message):
        return

    _processing.add(user_id)
    _log_request(user_id, "voice")

    try:
        # Download the voice file
        voice = message.voice
        file = await message.bot.get_file(voice.file_id)
        if not file.file_path:
            _processing.discard(user_id)
            await _reply_or_answer(message, "Could not access voice message.")
            return

        os.makedirs("/tmp/bot_downloads", exist_ok=True)
        local_path = os.path.join(
            "/tmp/bot_downloads", f"voice_{user_id}_{int(time.time())}.ogg",
        )
        await message.bot.download_file(file.file_path, destination=local_path)

        # Send processing status
        status_msg = await message.bot.send_message(
            message.chat.id, "🎤 Transcribing your voice message...",
        )

        # Transcribe
        transcribed = await LLMClient.transcribe_audio(local_path)
        if not transcribed:
            _processing.discard(user_id)
            await status_msg.edit_text(
                "❌ Could not transcribe your voice message. "
                "Please try sending it again or type your message instead."
            )
            return

        await status_msg.edit_text(f"🎤 \"{transcribed[:100]}{'...' if len(transcribed) > 100 else ''}\"")

        # Process the transcribed text as a normal message
        _last_user_message[user_id] = transcribed
        await _process_text_response(message, transcribed)

    except Exception as e:
        logger.error("[user=%d] Voice handler error: %s", user_id, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="voice_error",
            details=f"Voice processing failed: {e}",
        ))
        await _reply_or_answer(message, "Sorry, an error occurred while processing your voice message.")
    finally:
        _processing.discard(user_id)
        # Clean up temp file
        try:
            os.remove(local_path)
        except Exception:
            pass


# ─────────────────────────── Unsupported Message Handler ───────────────────────────

@router.message(~F.text)
async def handle_unsupported(message: Message) -> None:
    """Catch-all for unsupported message types (voice, video, sticker, etc.)."""
    # In groups, only respond to unsupported types if the user @mentioned the bot
    if not await _should_respond_in_group(message):
        return

    user_id = message.from_user.id

    blocked = await Database.is_user_blocked(user_id)
    if blocked:
        return

    if message.video or message.video_note:
        await _reply_or_answer(message,"Video messages are not supported. Please send text or a file.")
    elif message.sticker:
        await _reply_or_answer(message,"Sticker received! I can't process stickers, but feel free to send text.")
    elif message.location:
        await _reply_or_answer(message,"Location sharing is not supported. Please send text.")
    elif message.contact:
        await _reply_or_answer(message,"Contact sharing is not supported. Please send text.")
    elif message.animation:
        await _reply_or_answer(message,"GIF/Animation received! I can't process animations yet.")
    elif message.poll:
        await _reply_or_answer(message,"Polls are not supported. Please send text.")
    elif message.dice:
        await _reply_or_answer(message,"Dice received! 🎲")
    else:
        # Generic fallback
        await _reply_or_answer(message,"This message type is not supported. Please send text, files, or photos.")


# ─────────────────────────── Core Processing ───────────────────────────

async def _process_text_response(message: Message, user_text: str) -> str:
    """Process a user message through the LLM and return the full response text.

    Performance improvements over the original:
      1. Single streaming call — no separate non-streaming tool-detection round-trip
      2. In-memory context append — eliminates duplicate get_conversation_context
      3. Deferred token counting — usage recording is fire-and-forget
      4. Progressive stream editing — fast initial updates, then relaxed
      5. Smart model routing — simple queries use fast_model
    """
    user_id = message.from_user.id

    # Build context ONCE, then append user message in memory (fix #2)
    context, conv_id = await SessionManager.get_conversation_context(user_id)
    await SessionManager.add_to_conversation(conv_id, user_id, "user", user_text)
    context.append({"role": "user", "content": user_text})

    tools = get_tool_definitions()
    LLMClient.set_default_tools(tools)

    # Smart model routing (fix #5): short/simple -> fast model
    model = await _select_fast_model(user_text, user_id)

    await message.bot.send_chat_action(message.chat.id, "typing")

    full_response = ""
    telegram_msg: Message | None = None
    typing_task: asyncio.Task | None = None

    async def _keep_typing() -> None:
        try:
            while True:
                await asyncio.sleep(4)
                await message.bot.send_chat_action(message.chat.id, "typing")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    for iteration in range(MAX_TOOL_ITERATIONS):
        typing_task = None
        if iteration == 0:
            try:
                typing_task = asyncio.ensure_future(_keep_typing())
            except Exception:
                pass
        else:
            # Re-run context with tool results
            context, conv_id = await SessionManager.get_conversation_context(user_id)

        # Fix #1: Single streaming call — stream_and_collect returns both
        # content and tool_calls from one stream, no separate non-streaming call.
        try:
            streamed_text, tool_calls = await LLMClient.stream_and_collect(
                messages=context, tools=tools, model=model,
            )
        finally:
            if typing_task:
                typing_task.cancel()
                typing_task = None

        # Fix #3: Deferred token counting — fire-and-forget
        asyncio.ensure_future(_record_usage_async(
            user_id, json.dumps(context), streamed_text,
        ))

        if not tool_calls or any(tc[0] == "" and tc[1] == "error" for tc in tool_calls):
            # No tool calls — this is a direct response. Stream it to the user.
            if iteration == 0 and streamed_text:
                # First iteration: stream the response progressively
                full_response = await _stream_progressive(
                    message, streamed_text,
                )
            elif streamed_text:
                full_response = streamed_text

            if full_response:
                await SessionManager.save_assistant_response(conv_id, user_id, full_response)

            # Auto-title: if the conversation still has the default title,
            # fire-and-forget a lightweight LLM call to generate a short title.
            all_msgs = await Database.get_conversation_messages(conv_id, limit=None)
            if len(all_msgs) >= 3:
                # Check current title
                conv = await Database.get_conversation_by_id(conv_id)
                if conv and conv.get("title", "").strip().lower() in ("new conversation", ""):
                    asyncio.ensure_future(_auto_title_conv(conv_id, user_id, all_msgs))
                # Trigger compression asynchronously
                if len(all_msgs) > COMPRESS_AFTER_MESSAGES:
                    asyncio.ensure_future(SessionManager.compress_context(user_id, conv_id))
            break

        # Tool call iteration
        image_ctx_var.set(user_id)

        raw_tool_calls_data = [
            {"id": tc[0], "type": "function", "function": {"name": tc[1], "arguments": json.dumps(tc[2])}}
            for tc in tool_calls
        ]
        tool_calls_json = json.dumps(raw_tool_calls_data)

        tc_info = json.dumps([{"id": tc[0], "name": tc[1]} for tc in tool_calls])
        await SessionManager.save_assistant_response(
            conv_id, user_id,
            f"{streamed_text}\n[Calling tools: {tc_info}]" if streamed_text else f"[Calling tools: {tc_info}]",
            tool_calls_json=tool_calls_json,
        )

        # Execute all tools in parallel
        tool_tasks = [
            _execute_single_tool(conv_id, user_id, tc_id, tool_name, tool_args)
            for tc_id, tool_name, tool_args in tool_calls
            if tc_id  # skip error entries
        ]
        await asyncio.gather(*tool_tasks)

    else:
        # Exceeded max tool iterations — send a default response to the user
        if typing_task:
            typing_task.cancel()
        full_response = full_response or "I've completed multiple operations. Feel free to ask for more details."
        await _send_long_message(message, full_response)

    if not full_response:
        full_response = "I processed your request."
        await _send_long_message(message, full_response)

    return full_response


async def _auto_title_conv(conv_id: int, user_id: int, messages: list[dict]) -> None:
    """Fire-and-forget: ask the LLM to generate a short title for a conversation.

    Uses the first few messages to generate a concise 3-5 word title.
    Runs in the background so the user doesn't wait for it.
    """
    try:
        # Build a mini context from the messages
        lines = []
        for m in messages[:5]:
            role = m["role"].upper()
            content = m["content"][:150]
            lines.append(f"[{role}]: {content}")
        context = "\n".join(lines)

        title_prompt = [
            {"role": "system", "content": "Generate a short, descriptive title (3-5 words) for this conversation. Respond with ONLY the title, no quotes, no punctuation, no explanation."},
            {"role": "user", "content": f"Conversation:\n{context}\n\nTitle:"},
        ]

        llm_response = await LLMClient.chat_completion(
            messages=title_prompt,
            temperature=0.2,
            max_tokens=30,
            model=settings.fast_model,
        )
        title = llm_response.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if title and len(title) >= 3 and len(title) <= 60:
            # Clean up: remove quotes, trailing punctuation
            title = title.strip("\"'").strip(".,!?:;")
            if title:
                await Database.update_conversation_title(conv_id, title[:60])
                logger.info("[user=%d] Auto-titled conv %d: %s", user_id, conv_id, title)
    except Exception as e:
        logger.debug("Auto-title failed for conv %d: %s", conv_id, e)


async def _select_fast_model(user_text: str, user_id: int) -> str:
    """Smart model routing: simple queries use fast_model, complex ones use primary.

    Heuristic: short text (< 60 chars, no multi-sentence) with no question marks
    or complex punctuation → fast model. Everything else → user's preferred model.
    Uses SessionManager cache to avoid DB round-trip.
    """
    preferred = await SessionManager.get_preferred_model_cached(user_id)
    if preferred:
        return preferred

    text = user_text.strip()
    # Simple: short, single sentence, basic greeting/acknowledgment
    is_simple = (
        len(text) < 60
        and text.count(".") <= 1
        and text.count("?") <= 1
        and "\n" not in text
        and len(text.split()) <= 15
    )
    return settings.fast_model if is_simple else settings.near_ai_model


async def _record_usage_async(user_id: int, context_json: str, response_text: str) -> None:
    """Fire-and-forget token counting and usage recording.

    Runs in a background task so the user doesn't wait for analytics.
    """
    try:
        tokens_in = await LLMClient.count_tokens(context_json)
        tokens_out = await LLMClient.count_tokens(response_text)
        await Database.record_usage(
            user_id, tokens_in=tokens_in, tokens_out=tokens_out,
        )
        # Also report token spend to dashboard
        if tokens_in or tokens_out:
            total_tokens = tokens_in + tokens_out
            asyncio.ensure_future(report_token_spend(
                user_id=user_id,
                tokens=total_tokens,
                model=None,  # model info isn't available at this call site
            ))
    except Exception as e:
        logger.warning("[user=%d] usage recording failed: %s", user_id, e)


async def _stream_progressive(message: Message, full_text: str) -> str:
    """Send the LLM response as a single message at the bottom of the chat.

    Previously this used progressive editing (send short chunk → edit longer → edit final),
    but that caused ordering issues: edit_text keeps the message at its original position,
    so if the user sends a follow-up while streaming, the edited final response appears
    ABOVE the user's new message instead of below it.

    Now we send the full response at once. The typing indicator (send_chat_action)
    already provides immediate responsiveness.
    """
    if not full_text:
        return full_text

    await _reply_or_answer(message, full_text)
    return full_text


async def _execute_single_tool(
    conv_id: int, user_id: int, tc_id: str, tool_name: str, tool_args: dict[str, Any]
) -> None:
    try:
        result = await execute_tool(tool_name, tool_args)
        await SessionManager.save_tool_result(conv_id, user_id, tc_id, tool_name, result)
        asyncio.ensure_future(report_tool_usage(
            user_id=user_id, tool_name=tool_name, status="success",
        ))
    except Exception as e:
        await SessionManager.save_tool_result(
            conv_id, user_id, tc_id, tool_name, f"Tool '{tool_name}' failed: {e}",
        )
        asyncio.ensure_future(report_tool_usage(
            user_id=user_id, tool_name=tool_name, status="error", error=str(e),
        ))


# ─────────────────────────── Vision ───────────────────────────

async def _process_vision_response(
    message: Message, image_path: str, prompt: str, user_id: int,
) -> None:
    """Process an image using vision API. Sends the full response at once."""
    try:
        loop = asyncio.get_running_loop()

        def _read_image() -> str:
            with open(image_path, "rb") as f:
                data = f.read()
            ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "png"
            b64 = base64.b64encode(data).decode("utf-8")
            return f"data:image/{ext};base64,{b64}"

        data_uri = await loop.run_in_executor(None, _read_image)
        system_prompt = await SessionManager.get_system_prompt(user_id)

        # Typing indicator while processing
        async def _keep_typing() -> None:
            try:
                while True:
                    await asyncio.sleep(4)
                    await message.bot.send_chat_action(message.chat.id, "typing")
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        typing_task = None
        try:
            typing_task = asyncio.ensure_future(_keep_typing())

            client = LLMClient.get_client()
            raw_stream = await client.chat.completions.create(
                model=settings.near_ai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    },
                ],
                max_tokens=settings.max_response_tokens,
                stream=True,
            )

            full_text = ""
            async for chunk in raw_stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                text_piece = delta.content if delta else ""
                if not text_piece:
                    continue
                full_text += text_piece
        finally:
            if typing_task:
                typing_task.cancel()

        # Send the full response at once — no progressive editing
        if full_text:
            await _reply_or_answer(message, full_text)
        else:
            await _reply_or_answer(message, "I processed the image but have no specific response.")

        conv_id = (await SessionManager.get_conversation_context(user_id))[1]
        await SessionManager.save_assistant_response(conv_id, user_id, full_text)

        # Track usage — count system prompt + text prompt as input tokens
        vision_input = system_prompt + "\n" + prompt
        tokens_in = await LLMClient.count_tokens(vision_input)
        tokens_out = await LLMClient.count_tokens(full_text)
        await Database.record_usage(user_id, tokens_in=tokens_in, tokens_out=tokens_out)

    except Exception as e:
        logger.error("[user=%d] Vision error: %s", user_id, e)
        asyncio.ensure_future(report_error(
            user_id=user_id, error_type="vision_error",
            details=f"Vision analysis failed: {e}",
        ))
        await _reply_or_answer(message,"Failed to analyze image. Please try again with a different file.")


# ─────────────────────────── Utility ───────────────────────────

async def _reply_or_answer(message: Message, text: str, **kwargs) -> Message:
    """Send a message — threaded reply always, so responses are visually linked
    to the user's message regardless of ordering.

    Falls back to plain text if Markdown parsing fails.
    """
    try:
        return await message.reply(text, **kwargs)
    except Exception as e:
        if "can't parse entities" in str(e) and kwargs.get("parse_mode"):
            kwargs.pop("parse_mode")
            return await message.reply(text, **kwargs)


async def _reply_or_answer_photo(message: Message, photo: BufferedInputFile, **kwargs) -> Message:
    """Send a photo — threaded reply in groups, normal answer in DMs."""
    if _is_group_chat(message):
        return await message.reply_photo(photo, **kwargs)
    return await message.answer_photo(photo, **kwargs)


async def _send_long_message(message: Message, text: str) -> None:
    MAX_LEN = 4000

    async def _send(text_part: str) -> None:
        try:
            await _reply_or_answer(message, text_part, parse_mode="Markdown")
        except Exception:
            try:
                await _reply_or_answer(message, text_part)
            except Exception as e:
                logger.error("Failed to send message: %s", e)

    if len(text) <= MAX_LEN:
        await _send(text)
        return

    parts = []
    remaining = text
    while remaining:
        if len(remaining) <= MAX_LEN:
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, MAX_LEN)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, MAX_LEN)
        if split_at == -1:
            split_at = remaining.rfind(". ", 0, MAX_LEN + 1)
            if split_at > 0:
                split_at += 1
        if split_at <= 0:
            split_at = MAX_LEN
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]

    for i, part in enumerate(parts):
        header = f"*Part {i + 1}/{len(parts)}*\n\n" if len(parts) > 1 else ""
        await _send(f"{header}{part.strip()}")


def _chunk_lines(lines: list[str], chunk_size: int) -> list[list[str]]:
    """Split a list of lines into chunks of at most chunk_size lines."""
    return [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]