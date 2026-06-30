"""
Premium multi-page inline menu for Bud AI Telegram Bot.

Provides a polished, state-aware menu system with:
- Multi-page navigation with dynamic header
- Action flow: tap button → input prompt → execute → return to menu
- Live stats in the header (active conversation, daily usage)
- Admin panel (visible only to admins)
- Pending action state for tools requiring text input
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app.database import Database
from app.session_manager import SessionManager, bust_cache
from app.skills.registry import execute_tool
from app.skills.image_gen import pop_pending_images

logger = logging.getLogger(__name__)

# ─── Expose router so it can be included by bot.py ───
menu_router = Router()

# ─── Constants ───
MENU_PREFIX = "menu"
PAGE_MAIN = "main"
PAGE_TOOLS = "tools"
PAGE_CONVERSATIONS = "convs"
PAGE_SETTINGS = "settings"
PAGE_ADMIN = "admin"
PAGE_ADMIN_MODEL = "admin_model"
PAGE_ADMIN_FEEDBACK = "admin_feedback"
PAGE_ADMIN_WEBSITE = "admin_website"
PAGE_ADMIN_GROUPLINK = "admin_grouplink"
PAGE_ADMIN_PASSCODE = "admin_passcode"
PAGE_ADMIN_REMOVE_PASSCODE = "admin_remove_passcode"
PAGE_ADMIN_STATS = "admin_stats_page"
PAGE_ADMIN_USERS = "admin_users_page"
PAGE_ADMIN_VIEWCONV = "admin_viewconv"
PAGE_ADMIN_LLM_CONFIG = "admin_llm_config"
PAGE_ADMIN_DIAG = "admin_diag"
PAGE_ADMIN_ANALYTICS = "admin_analytics"
PAGE_ADMIN_SKILLS = "admin_skills"
PAGE_ADMIN_BROADCAST = "admin_broadcast"
PAGE_ADMIN_BLOCK = "admin_block"
PAGE_ADMIN_UNBLOCK = "admin_unblock"

# ─── Per-user state ───
_current_page: dict[int, str] = {}
_pending_action: dict[int, str] = {}  # "web_search", "generate_image", "calculate", "set_prompt", etc.

# Pinned menu message tracker — stores the single menu message per user so
# all menu edits happen in-place, never creating duplicate messages.
_menu_tracker: dict[int, dict] = {}  # user_id -> {"chat_id": int, "message_id": int}


# ═══════════════════════════════════════════════════════════════
#  Menu Page Builders
# ═══════════════════════════════════════════════════════════════

def _page_indicator(pages: list[str], current: str) -> str:
    """Build a page indicator like ● ○ ○ ○."""
    idx = pages.index(current) if current in pages else 0
    return " ".join("●" if i == idx else "○" for i in range(len(pages)))


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{MENU_PREFIX}:{data}")


async def _build_header(user_id: int) -> str:
    """Build the dynamic header showing active conversation and today's stats."""
    conv = await Database.get_active_conversation(user_id)
    conv_title = conv.get("title", "New conversation")[:30] if conv else "None"
    today = await Database.get_user_usage_today(user_id)
    reqs = today.get("requests", 0)
    imgs = today.get("images_generated", 0)

    if conv:
        msgs = await Database.get_conversation_messages(conv["id"], limit=None)
        msg_count = len(msgs)
    else:
        msg_count = 0

    return (
        f"🤖 *N0M3RCi Main Menu*\n\n"
        f"💬 `{conv_title}` · {msg_count} msgs\n"
        f"📊 Today: {reqs} req · {imgs} imgs"
    )


async def build_main_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Main dashboard — overview + quick actions."""
    header = await _build_header(user_id)
    text = (
        f"{header}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )

    builder = InlineKeyboardBuilder()
    # Row 1: Web Search + Image Gen
    builder.row(
        _button("🔍  Web Search", "action:web_search"),
        _button("🎨  Image Gen", "action:generate_image"),
        width=2,
    )
    # Row 2: Math + Read File
    builder.row(
        _button("🧮  Calculate", "action:calculate"),
        _button("📁  Read File", "action:read_file"),
        width=2,
    )
    # Row 3: Conversations + Stats
    builder.row(
        _button("💬  Conversations", f"page:{PAGE_CONVERSATIONS}"),
        _button("📊  My Stats", "action:stats"),
        width=2,
    )
    # Row 4: Settings + New Chat
    builder.row(
        _button("⚙️  Settings", f"page:{PAGE_SETTINGS}"),
        _button("🆕  New Chat", "action:new_chat"),
        width=2,
    )
    # Row 5: Retry + Help
    builder.row(
        _button("🔄  Retry", "action:retry"),
        _button("❓  Help", "action:help"),
        width=2,
    )

    # Website and Group links (if configured)
    website_url = await Database.get_config("website_url")
    group_link = await Database.get_config("group_link")
    link_row_buttons = []
    if website_url:
        link_row_buttons.append(_button("🌐  Website", "action:open_website"))
    if group_link:
        link_row_buttons.append(_button("👥  Group", "action:open_group"))
    if link_row_buttons:
        builder.row(*link_row_buttons, width=len(link_row_buttons))

    # Admin button (only for admins)
    is_admin = user_id in settings.admin_ids
    if is_admin:
        builder.row(
            _button("🛠️  Admin Panel", f"page:{PAGE_ADMIN}"),
            width=1,
        )

    indicator = _page_indicator(
        [PAGE_MAIN, PAGE_TOOLS, PAGE_CONVERSATIONS, PAGE_SETTINGS] + (["admin"] if is_admin else []),
        PAGE_MAIN,
    )
    text += f"\n\n{indicator}"

    return text, builder.as_markup()


async def build_tools_page() -> tuple[str, InlineKeyboardMarkup]:
    """Tools page — detailed tools with descriptions."""
    text = (
        "🧰 *Tools*\n\n"
        "Choose a tool to use:\n\n"
        "🔍 *Web Search* — Search the web for current info\n"
        "🎨 *Generate Image* — Create an image from text\n"
        "🧮 *Calculate* — Solve math expressions\n"
        "📁 *Read File* — Analyze uploaded files"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("🔍  Search", "action:web_search"),
        _button("🎨  Image", "action:generate_image"),
        width=2,
    )
    builder.row(
        _button("🧮  Math", "action:calculate"),
        _button("📁  File", "action:read_file"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back", f"page:{PAGE_MAIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_MAIN, PAGE_TOOLS], PAGE_TOOLS)}"
    return text, builder.as_markup()


async def build_conversations_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Conversations page — list all conversations with tap to switch."""
    convs = await Database.list_conversations(user_id)

    if not convs:
        text = "💬 *Conversations*\n\nNo conversations yet."
        builder = InlineKeyboardBuilder()
        builder.row(
            _button("🆕  Start New Chat", "action:new_chat"),
            _button("⬅️  Back", f"page:{PAGE_MAIN}"),
            width=1,
        )
        return text, builder.as_markup()

    # Get active conversation to highlight it
    active = await Database.get_active_conversation(user_id)
    active_id = active["id"] if active else None

    text = "💬 *Conversations*\n\nTap to switch:\n"
    builder = InlineKeyboardBuilder()

    for conv in convs:
        title = conv.get("title", "Untitled")[:25]
        msg_count = conv.get("msg_count", 0)
        is_active = conv["id"] == active_id
        prefix = "● " if is_active else ""
        label = f"{prefix}{title} ({msg_count})"
        builder.button(text=label, callback_data=f"{MENU_PREFIX}:conv_switch:{conv['id']}")

    builder.adjust(1)

    # Management row
    builder.row(
        _button("🆕  New", "action:new_chat"),
        _button("📤  Export", "action:export_conv"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back", f"page:{PAGE_MAIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_MAIN, PAGE_CONVERSATIONS], PAGE_CONVERSATIONS)}"
    return text, builder.as_markup()


async def build_settings_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Settings page — view and change user settings."""
    user = await Database.get_user_settings(user_id)
    if not user:
        text = "⚙️ *Settings*\n\nNo settings found. Send /start first."
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back", f"page:{PAGE_MAIN}"), width=1)
        return text, builder.as_markup()

    system_prompt = (user.get("system_prompt", "Default") or "Default")[:60]
    max_msgs = user.get("max_context_messages", 50)
    max_tokens = user.get("max_context_tokens", 128000)

    text = (
        f"⚙️ *Settings*\n\n"
        f"🧠 *System Prompt:*\n`{system_prompt}...`\n\n"
        f"📝 Max Messages: `{max_msgs}`\n"
        f"🔢 Max Tokens: `{max_tokens:,}`\n"
        f"📅 Daily Limit: `{settings.rate_limit_daily_cap}` req"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("✏️  Change Prompt", "action:set_prompt"),
        _button("📊  My Stats", "action:stats"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back", f"page:{PAGE_MAIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_MAIN, PAGE_SETTINGS], PAGE_SETTINGS)}"
    return text, builder.as_markup()


async def build_admin_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Premium BotFather-style admin master dashboard with emoji-rich command listing."""
    # ── Gather live stats ──
    try:
        total_users = await Database.get_total_user_count()
    except Exception:
        total_users = 0
    try:
        today_usage = await Database.get_all_users_usage_today()
        total_reqs = sum(u.get("requests", 0) for u in today_usage)
    except Exception:
        total_reqs = 0

    uptime_hours = 0
    try:
        import app.router as _router
        import time
        uptime_hours = int(time.time() - _router._bot_start_time) // 3600
    except Exception:
        pass

    db_ok = await Database.ping()
    db_status = "🟢 Online" if db_ok else "🔴 Offline"
    current_model = (await Database.get_preferred_model(user_id)) or settings.near_ai_model
    try:
        all_feedback = await Database.get_all_feedback(limit=1000)
        feedback_count = len(all_feedback)
    except Exception:
        feedback_count = 0

    # Config statuses
    website_url = await Database.get_config("website_url")
    group_link = await Database.get_config("group_link")
    ws_status = "✅" if website_url else "❌"
    grp_status = "✅" if group_link else "❌"

    # ── Build the BotFather-style dashboard ──
    text = (
        "🛠️ *ADMIN MASTER PANEL*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 `{total_users}` users    📊 `{total_reqs}` today    ⏱️ `{uptime_hours}h`\n"
        f"💾 DB: {db_status}    🧠 `{current_model[:30]}`\n"
        f"💬 Feedback: `{feedback_count}`    🌐 {ws_status} Web    👥 {grp_status} Grp\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "╔══ BotFather-Style Admin Commands ══╗\n\n"
        "👥 *USER MANAGEMENT*\n"
        "  `/admin_users` 👥 List all users\n"
        "  `/admin_viewconv` 🔍 Inspect user & conversations\n"
        "  `/admin_block` 🚫 Block a user\n"
        "  `/admin_unblock` ✅ Unblock a user\n\n"
        "📊 *ANALYTICS*\n"
        "  `/admin_stats` 📊 Bot usage statistics\n"
        "  `/admin_analytics` 📈 Usage analytics & CSV export\n"
        "  `/admin_diag` 🩺 System diagnostics dashboard\n\n"
        "⚙️ *CONFIGURATION*\n"
        "  `/admin_llm_config` ⚙️ LLM runtime configuration\n"
        "  `/admin_skills` 🔧 View tool & skill registry\n"
        "  `Change Model` 🧠 Switch AI model\n\n"
        "💬 *COMMUNICATION*\n"
        "  `/admin_broadcast` 📢 Send broadcast to all users\n"
        "  `/admin_feedback` 💬 View user feedback\n\n"
        "🔒 *SECURITY*\n"
        "  `Set Passcode` 🔒 Lock a group with passcode\n"
        "  `Remove Passcode` 🔓 Unlock a group\n\n"
        "🌐 *LINKS*\n"
        "  `Website Link` 🌐 Configure website URL\n"
        "  `Group Link` 👥 Configure group invite link\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )

    builder = InlineKeyboardBuilder()

    # Row 1: User Management
    builder.row(
        _button("👥  Users", f"page:{PAGE_ADMIN_USERS}"),
        _button("🔍  View User", "action:admin_viewconv"),
        _button("🚫  Block", "action:admin_block"),
        width=3,
    )
    # Row 2: Analytics
    builder.row(
        _button("📊  Stats", "action:admin_stats"),
        _button("📈  Analytics", f"page:{PAGE_ADMIN_ANALYTICS}"),
        _button("🩺  Diagnostics", f"page:{PAGE_ADMIN_DIAG}"),
        width=3,
    )
    # Row 3: Configuration
    builder.row(
        _button("🧠  Model", f"page:{PAGE_ADMIN_MODEL}"),
        _button("⚙️  LLM Config", f"page:{PAGE_ADMIN_LLM_CONFIG}"),
        _button("🔧  Skills", f"page:{PAGE_ADMIN_SKILLS}"),
        width=3,
    )
    # Row 4: Communication
    builder.row(
        _button("📢  Broadcast", "action:admin_broadcast"),
        _button("💬  Feedback", f"page:{PAGE_ADMIN_FEEDBACK}"),
        width=2,
    )
    # Row 5: Security
    builder.row(
        _button("🔒  Set Passcode", f"page:{PAGE_ADMIN_PASSCODE}"),
        _button("🔓  Remove Passcode", f"page:{PAGE_ADMIN_REMOVE_PASSCODE}"),
        width=2,
    )
    # Row 6: External Links
    builder.row(
        _button("🌐  Website", f"page:{PAGE_ADMIN_WEBSITE}"),
        _button("👥  Group", f"page:{PAGE_ADMIN_GROUPLINK}"),
        width=2,
    )
    # Row 7: Back
    builder.row(
        _button("⬅️  Back to Main Menu", f"page:{PAGE_MAIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_MAIN, PAGE_ADMIN], PAGE_ADMIN)}"
    return text, builder.as_markup()


async def build_admin_model_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Model management page — view and switch AI models."""
    current_model = await Database.get_preferred_model(user_id) or settings.near_ai_model

    text = (
        f"🧠 *Model Management*\n\n"
        f"Current: `{current_model}`\n\n"
        f"Select a model to switch:"
    )

    builder = InlineKeyboardBuilder()
    for model_name in settings.available_models:
        marker = "● " if model_name == current_model else ""
        builder.button(
            text=f"{marker}{model_name}",
            callback_data=f"{MENU_PREFIX}:admin_switch_model:{model_name}",
        )
    builder.adjust(1)
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_ADMIN, PAGE_ADMIN_MODEL], PAGE_ADMIN_MODEL)}"
    return text, builder.as_markup()


async def build_admin_feedback_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Feedback management page — view and clear user feedback."""
    all_feedback = await Database.get_all_feedback(limit=20)

    if not all_feedback:
        text = "💬 *Feedback*\n\nNo feedback recorded yet."
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"), width=1)
        text += f"\n\n{_page_indicator([PAGE_ADMIN, PAGE_ADMIN_FEEDBACK], PAGE_ADMIN_FEEDBACK)}"
        return text, builder.as_markup()

    lines = ["💬 *User Feedback*", ""]
    for fb in all_feedback:
        name = fb.get("first_name") or fb.get("username") or str(fb["user_id"])
        ts = fb["created_at"].strftime("%m-%d %H:%M") if fb.get("created_at") else "?"
        msg_text = fb["message"][:150]
        lines.append(f"• *{name}* [{ts}]: _{msg_text}_")

    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("🗑️  Clear All Feedback", "action:admin_clear_feedback"),
        width=1,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_ADMIN, PAGE_ADMIN_FEEDBACK], PAGE_ADMIN_FEEDBACK)}"
    return text, builder.as_markup()


async def build_admin_website_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Website link configuration page."""
    current_url = await Database.get_config("website_url") or "Not set"

    text = (
        f"🌐 *Website Link*\n\n"
        f"Current: `{current_url}`\n\n"
        f"Tap *Set Link* to enter a new URL, or *Remove* to clear it."
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("✏️  Set Link", "action:set_website"),
        _button("🗑️  Remove", "action:admin_remove_website"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_ADMIN, PAGE_ADMIN_WEBSITE], PAGE_ADMIN_WEBSITE)}"
    return text, builder.as_markup()


async def build_admin_grouplink_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Group link configuration page."""
    current_link = await Database.get_config("group_link") or "Not set"

    text = (
        f"👥 *Group Link*\n\n"
        f"Current: `{current_link}`\n\n"
        f"Tap *Set Link* to enter a new URL, or *Remove* to clear it."
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("✏️  Set Link", "action:set_grouplink"),
        _button("🗑️  Remove", "action:admin_remove_grouplink"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_ADMIN, PAGE_ADMIN_GROUPLINK], PAGE_ADMIN_GROUPLINK)}"
    return text, builder.as_markup()


async def build_admin_passcode_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Set passcode page."""
    text = (
        "🔒 *Set Group Passcode*\n\n"
        "Tap *Set Passcode* and send the group ID and passcode in this format:\n\n"
        "`<group_id> <passcode>`\n\n"
        "Example: `-100123456789 mysecretpass`\n\n"
        "The bot must be an admin in the group with *restrict members* permission."
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("🔒  Set Passcode", "action:set_passcode"),
        width=1,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_ADMIN, PAGE_ADMIN_PASSCODE], PAGE_ADMIN_PASSCODE)}"
    return text, builder.as_markup()


async def build_admin_remove_passcode_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Remove passcode page."""
    text = (
        "🔓 *Remove Group Passcode*\n\n"
        "Tap *Remove Passcode* and send the group ID:\n\n"
        "`<group_id>`\n\n"
        "Example: `-100123456789`"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("🔓  Remove Passcode", "action:remove_passcode"),
        width=1,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )

    text += f"\n\n{_page_indicator([PAGE_ADMIN, PAGE_ADMIN_REMOVE_PASSCODE], PAGE_ADMIN_REMOVE_PASSCODE)}"
    return text, builder.as_markup()


# ═══════════════════════════════════════════════════════════════
#  Premium Admin Sub-Pages — BotFather-Style Detail Views
# ═══════════════════════════════════════════════════════════════


async def build_admin_stats_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """📊 Detailed admin statistics page."""
    if user_id not in settings.admin_ids:
        text = "⛔ *Access Denied*"
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"), width=1)
        return text, builder.as_markup()

    try:
        total_users = await Database.get_total_user_count()
    except Exception:
        total_users = "?"
    try:
        total_convs = await Database.get_total_messages_count()
        total_convs = f"{total_convs:,}"
    except Exception:
        total_convs = "?"
    try:
        today_usage = await Database.get_all_users_usage_today()
        total_reqs = sum(u.get("requests", 0) for u in today_usage)
        total_tokens_in = sum(u.get("tokens_in", 0) for u in today_usage)
        total_tokens_out = sum(u.get("tokens_out", 0) for u in today_usage)
    except Exception:
        total_reqs = total_tokens_in = total_tokens_out = "?"

    active_now = 0
    try:
        import app.router as _router
        active_now = len(_router._processing)
    except Exception:
        pass

    uptime_secs = 0
    try:
        import app.router as _router
        import time
        uptime_secs = int(time.time() - _router._bot_start_time)
    except Exception:
        pass

    uptime_h = uptime_secs // 3600
    uptime_m = (uptime_secs % 3600) // 60
    uptime_d = uptime_h // 24
    if uptime_d > 0:
        uptime_str = f"{uptime_d}d {uptime_h % 24}h {uptime_m}m"
    else:
        uptime_str = f"{uptime_h}h {uptime_m}m"

    text = (
        "📊 *Admin Statistics*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 *Users:* `{total_users}`\n"
        f"💬 *Messages:* `{total_convs}`\n"
        f"⚡ *Active Now:* `{active_now}`\n"
        f"⏱️ *Uptime:* `{uptime_str}`\n\n"
        "📈 *Today's Activity*\n"
        f"  📥 Requests: `{total_reqs}`\n"
        f"  🔢 Tokens In: `{total_tokens_in:,}`\n"
        f"  📤 Tokens Out: `{total_tokens_out:,}`\n\n"
        "⚙️ *Configuration*\n"
        f"  🧠 Model: `{settings.near_ai_model}`\n"
        f"  📸 Image: `{settings.near_ai_image_model}`\n"
        f"  🚦 Rate Limit: `{settings.rate_limit_max_requests}`/{settings.rate_limit_window_seconds}s\n"
        f"  📅 Daily Cap: `{settings.rate_limit_daily_cap}` req/user"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("📈  Analytics", f"page:{PAGE_ADMIN_ANALYTICS}"),
        _button("🩺  Diagnostics", f"page:{PAGE_ADMIN_DIAG}"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )
    return text, builder.as_markup()


async def build_admin_users_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """👥 Users management page with live processing info."""
    if user_id not in settings.admin_ids:
        text = "⛔ *Access Denied*"
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"), width=1)
        return text, builder.as_markup()

    try:
        total_users = await Database.get_total_user_count()
    except Exception:
        total_users = "?"

    active_now = []
    try:
        import app.router as _router
        active_now = list(_router._processing)
    except Exception:
        pass

    text = (
        "👥 *User Management*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Total Users:* `{total_users}`\n"
        f"⚡ *Currently Processing:* `{len(active_now)}`\n"
    )

    if active_now:
        text += "\n*Active Users:*\n"
        for uid in active_now[:10]:
            text += f"  • `{uid}`\n"
        if len(active_now) > 10:
            text += f"  • *...and {len(active_now) - 10} more*\n"

    text += "\n" + "━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += "*Actions:*\n"
    text += "🔍 `/admin_viewconv <id>` — Inspect user\n"
    text += "🚫 `/admin_block <id>` — Block a user\n"
    text += "✅ `/admin_unblock <id>` — Unblock a user"

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("🔍  View User", "action:admin_viewconv"),
        _button("🚫  Block", "action:admin_block"),
        _button("✅  Unblock", "action:admin_unblock"),
        width=3,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )
    return text, builder.as_markup()


async def build_admin_diag_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """🩺 Real-time diagnostics page."""
    if user_id not in settings.admin_ids:
        text = "⛔ *Access Denied*"
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"), width=1)
        return text, builder.as_markup()

    uptime_secs = 0
    try:
        import app.router as _router
        import time
        uptime_secs = int(time.time() - _router._bot_start_time)
    except Exception:
        pass
    uptime_h = uptime_secs // 3600
    uptime_m = (uptime_secs % 3600) // 60

    db_stats = await Database.get_db_pool_stats()
    try:
        total_users = await Database.get_total_user_count()
    except Exception:
        total_users = "?"
    try:
        pool = await Database.get_pool()
        async with pool.acquire() as conn:
            total_convs = await conn.fetchval("SELECT COUNT(*) FROM conversations")
    except Exception:
        total_convs = "?"
    try:
        total_msgs = await Database.get_total_messages_count()
    except Exception:
        total_msgs = "?"

    try:
        import app.router as _router
        active_processing = len(_router._processing)
        pending_passcodes = len(_router._pending_passcode)
        cache_entries = len(_router._last_user_message)
    except Exception:
        active_processing = pending_passcodes = cache_entries = "?"

    db_pool_str = f"{db_stats.get('free', 0)}/{db_stats.get('pool_size', 0)} free"
    db_status = "🟢 Connected" if db_stats.get("status") == "connected" else "🔴 Disconnected"

    text = (
        "🩺 *System Diagnostics*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ *Uptime:* `{uptime_h}h {uptime_m}m`\n"
        f"💾 *DB Pool:* `{db_pool_str}` — {db_status}\n\n"
        "📊 *Database Counters*\n"
        f"  👥 Users: `{total_users}`\n"
        f"  💬 Conversations: `{total_convs}`\n"
        f"  📝 Messages: `{total_msgs}`\n\n"
        "🧠 *In-Memory State*\n"
        f"  ⚡ Processing: `{active_processing}`\n"
        f"  🔐 Pending Passcodes: `{pending_passcodes}`\n"
        f"  💾 Cached Messages: `{cache_entries}`\n\n"
        "⚙️ *Runtime*\n"
        f"  🧠 Model: `{settings.near_ai_model}`\n"
        f"  🚦 Rate Limit: `{settings.rate_limit_max_requests}`/{settings.rate_limit_window_seconds}s\n"
        f"  📅 Daily Cap: `{settings.rate_limit_daily_cap}` req\n"
        f"  🌐 Webhook: `{settings.use_webhook}`"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("📊  Stats", f"page:{PAGE_ADMIN_STATS}"),
        _button("🔄  Refresh", f"page:{PAGE_ADMIN_DIAG}"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )
    return text, builder.as_markup()


async def build_admin_analytics_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """📈 Usage analytics overview page."""
    if user_id not in settings.admin_ids:
        text = "⛔ *Access Denied*"
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"), width=1)
        return text, builder.as_markup()

    try:
        analytics = await Database.get_usage_analytics(7)
    except Exception:
        analytics = {}

    total_reqs = analytics.get("total_requests", 0)
    active_users = analytics.get("active_users", 0)
    tokens_in = analytics.get("tokens_in", 0)
    tokens_out = analytics.get("tokens_out", 0)
    images = analytics.get("images_generated", 0)
    daily = analytics.get("daily_breakdown", [])
    top = analytics.get("top_users", [])

    text = (
        "📈 *Usage Analytics — Last 7 Days*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Total Requests:* `{total_reqs:,}`\n"
        f"👥 *Active Users:* `{active_users}`\n"
        f"🔢 *Tokens In:* `{tokens_in:,}`\n"
        f"📤 *Tokens Out:* `{tokens_out:,}`\n"
        f"🖼️ *Images:* `{images}`\n\n"
    )

    if daily:
        text += "*Daily Breakdown:*\n"
        for d in daily[:7]:
            text += f"  `{d['date']}` — {d['reqs']} req, {d['t_in']} in, {d['t_out']} out\n"
        text += "\n"

    if top:
        text += "*Top Users:*\n"
        for i, u in enumerate(top[:5], 1):
            uname = u.get("username") or f"user_{u['user_id']}"
            text += f"  {i}. `{u['user_id']}` (@{uname}) — {u['reqs']} req\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("📊  Stats", f"page:{PAGE_ADMIN_STATS}"),
        _button("🩺  Diagnostics", f"page:{PAGE_ADMIN_DIAG}"),
        width=2,
    )
    builder.row(
        _button("📥  Export CSV (7d)", "action:admin_export_csv"),
        width=1,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )
    return text, builder.as_markup()


async def build_admin_llm_config_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """⚙️ LLM configuration management page."""
    if user_id not in settings.admin_ids:
        text = "⛔ *Access Denied*"
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"), width=1)
        return text, builder.as_markup()

    overrides = {}
    for k in ("temperature", "max_tokens", "model", "image_model", "image_size"):
        val = await Database.get_config(f"llm_{k}")
        if val:
            overrides[k] = val

    text = (
        "⚙️ *LLM Configuration*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "*Default (Environment):*\n"
        f"  🧠 Model: `{settings.near_ai_model}`\n"
        f"  📸 Image Model: `{settings.near_ai_image_model}`\n"
        f"  📐 Image Size: `{settings.near_ai_image_size}`\n"
        f"  📏 Max Tokens: `{settings.max_response_tokens:,}`\n"
        f"  🔗 Base URL: `{settings.near_ai_base_url}`\n"
    )

    if overrides:
        text += "\n*⚡ Runtime Overrides:*\n"
        for k, v in overrides.items():
            text += f"  • `{k}` = `{v}`\n"
    else:
        text += "\n*No runtime overrides set.*\n"

    text += "\n" + "━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += "*Commands:*\n"
    text += "  `/admin_llm_config set <key> <value>`\n"
    text += "  `/admin_llm_config reset <key>`\n"
    text += "*Valid keys:* temperature, max_tokens, model, image_model, image_size"

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("🧠  Change Model", f"page:{PAGE_ADMIN_MODEL}"),
        _button("🔧  Skills", f"page:{PAGE_ADMIN_SKILLS}"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )
    return text, builder.as_markup()


async def build_admin_skills_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """🔧 Tool/skill registry management page."""
    if user_id not in settings.admin_ids:
        text = "⛔ *Access Denied*"
        builder = InlineKeyboardBuilder()
        builder.row(_button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"), width=1)
        return text, builder.as_markup()

    from app.skills.registry import _TOOL_HANDLERS, ALL_TOOL_DEFINITIONS

    text = (
        "🔧 *Skill Registry Manager*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Registered Tools:* `{len(_TOOL_HANDLERS)}`\n\n"
    )

    for tool_def in ALL_TOOL_DEFINITIONS:
        name = tool_def.get("function", {}).get("name", "unknown")
        desc = tool_def.get("function", {}).get("description", "—")
        params = tool_def.get("function", {}).get("parameters", {}).get("properties", {})
        required = tool_def.get("function", {}).get("parameters", {}).get("required", [])

        has_handler = "✅" if name in _TOOL_HANDLERS else "❌"
        text += f"{has_handler} `{name}`\n"
        text += f"   📝 {desc[:100]}\n"
        if params:
            text += f"   📥 Params: {', '.join(f'`{k}`' for k in params.keys())}\n"
        if required:
            text += f"   ⚠️ Required: {', '.join(f'`{k}`' for k in required)}\n"
        text += "\n"

    handler_names = list(_TOOL_HANDLERS.keys())
    defined_names = [t.get("function", {}).get("name", "") for t in ALL_TOOL_DEFINITIONS]
    orphaned = [n for n in handler_names if n not in defined_names]
    if orphaned:
        text += f"*⚠️ Orphaned Handlers:* {', '.join(orphaned)}\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        _button("⚙️  LLM Config", f"page:{PAGE_ADMIN_LLM_CONFIG}"),
        _button("🧠  Change Model", f"page:{PAGE_ADMIN_MODEL}"),
        width=2,
    )
    builder.row(
        _button("⬅️  Back to Admin", f"page:{PAGE_ADMIN}"),
        width=1,
    )
    return text, builder.as_markup()


# ═══════════════════════════════════════════════════════════════
#  Input Prompt Builder
# ═══════════════════════════════════════════════════════════════

async def build_input_prompt(user_id: int, action: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build an input prompt page asking the user to type input."""
    prompts = {
        "web_search": (
            "🔍 *Web Search*\n\n"
            "Please send the search query as a text message.\n\n"
            "Example: `latest AI news 2026`"
        ),
        "generate_image": (
            "🎨 *Generate Image*\n\n"
            "Please send a detailed image description as a text message.\n\n"
            "Example: `A sunset over a futuristic city, digital art`"
        ),
        "calculate": (
            "🧮 *Calculate*\n\n"
            "Please send a math expression as a text message.\n\n"
            "Examples: `2 + 2`, `sin(pi/4)`, `sqrt(144)`"
        ),
        "read_file": (
            "📁 *Read File*\n\n"
            "Please upload the file you want me to analyze.\n\n"
            "Supported: PDF, images, text files, code, JSON, CSV"
        ),
        "set_prompt": (
            "✏️ *Set System Prompt*\n\n"
            "Please send your new system prompt as a text message.\n\n"
            "This will replace your current AI behavior instructions."
        ),
        "admin_broadcast": (
            "📢 *Admin Broadcast*\n\n"
            "Please send the broadcast message you want to send to all users."
        ),
        "admin_block": (
            "🚫 *Block User*\n\n"
            "Please send the user ID you want to block.\n\n"
            "Example: `123456789`"
        ),
        "admin_unblock": (
            "✅ *Unblock User*\n\n"
            "Please send the user ID you want to unblock.\n\n"
            "Example: `123456789`"
        ),
        "set_website": (
            "🌐 *Set Website Link*\n\n"
            "Please send the full website URL.\n\n"
            "Example: `https://example.com`"
        ),
        "set_grouplink": (
            "👥 *Set Group Link*\n\n"
            "Please send the group invite link.\n\n"
            "Example: `https://t.me/yourgroup`"
        ),
        "set_passcode": (
            "🔒 *Set Group Passcode*\n\n"
            "Send the group ID and passcode in this format:\n\n"
            "`<group_id> <passcode>`\n\n"
            "Example: `-100123456789 mysecretpass`"
        ),
        "remove_passcode": (
            "🔓 *Remove Group Passcode*\n\n"
            "Send the group ID to remove its passcode:\n\n"
            "`<group_id>`\n\n"
            "Example: `-100123456789`"
        ),
        "admin_viewconv": (
            "🔍 *View User* — Inspect User Profile & Conversations\n\n"
            "Please send the user ID you want to inspect.\n\n"
            "Example: `123456789`\n\n"
            "Shows profile details, stats, and recent conversations."
        ),
        "admin_clear_feedback": (
            "🗑️ *Clear All Feedback*\n\n"
            "Are you sure you want to delete all feedback?\n"
            "Send `yes` to confirm, or anything else to cancel."
        ),
    }

    text = prompts.get(action, f"Please send the required information for: `{action}`")
    builder = InlineKeyboardBuilder()
    builder.row(_button("⬅️  Cancel", "action:cancel_input"), width=1)
    return text, builder.as_markup()


# ═══════════════════════════════════════════════════════════════
#  Action Handlers — these execute the actual backend logic
# ═══════════════════════════════════════════════════════════════

async def _show_main_menu(message_or_callback: Message | CallbackQuery, user_id: int) -> None:
    """Show the main menu — always edits in-place, never sends a new message.

    Stores the single menu message in _menu_tracker so all subsequent
    actions can edit the same pinned message.
    """
    text, markup = await build_main_page(user_id)
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
            _menu_tracker[user_id] = {
                "chat_id": message_or_callback.message.chat.id,
                "message_id": message_or_callback.message.message_id,
            }
        except Exception:
            pass  # Silently ignore — menu message may have been deleted
        await message_or_callback.answer()
    else:
        sent = await message_or_callback.answer(text, parse_mode="Markdown", reply_markup=markup)
        _menu_tracker[user_id] = {
            "chat_id": sent.chat.id,
            "message_id": sent.message_id,
        }


async def _build_page_for_page(page: str, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build a menu page by name — same builder functions as _navigate_page."""
    if page == PAGE_MAIN:
        return await build_main_page(user_id)
    elif page == PAGE_TOOLS:
        return await build_tools_page()
    elif page == PAGE_CONVERSATIONS:
        return await build_conversations_page(user_id)
    elif page == PAGE_SETTINGS:
        return await build_settings_page(user_id)
    elif page == PAGE_ADMIN:
        return await build_admin_page(user_id)
    elif page == PAGE_ADMIN_MODEL:
        return await build_admin_model_page(user_id)
    elif page == PAGE_ADMIN_FEEDBACK:
        return await build_admin_feedback_page(user_id)
    elif page == PAGE_ADMIN_WEBSITE:
        return await build_admin_website_page(user_id)
    elif page == PAGE_ADMIN_GROUPLINK:
        return await build_admin_grouplink_page(user_id)
    elif page == PAGE_ADMIN_PASSCODE:
        return await build_admin_passcode_page(user_id)
    elif page == PAGE_ADMIN_REMOVE_PASSCODE:
        return await build_admin_remove_passcode_page(user_id)
    elif page == PAGE_ADMIN_STATS:
        return await build_admin_stats_page(user_id)
    elif page == PAGE_ADMIN_USERS:
        return await build_admin_users_page(user_id)
    elif page == PAGE_ADMIN_DIAG:
        return await build_admin_diag_page(user_id)
    elif page == PAGE_ADMIN_ANALYTICS:
        return await build_admin_analytics_page(user_id)
    elif page == PAGE_ADMIN_LLM_CONFIG:
        return await build_admin_llm_config_page(user_id)
    elif page == PAGE_ADMIN_SKILLS:
        return await build_admin_skills_page(user_id)
    else:
        return await build_main_page(user_id)


async def _return_to_page(message: Message, user_id: int) -> None:
    """Return to the user's current menu page by editing the pinned menu message.

    Uses _menu_tracker to find the single menu message — never sends a new one.
    The `message` parameter is only used for `message.bot` (Telegram API client).
    """
    page = _current_page.get(user_id, PAGE_MAIN)
    text, markup = await _build_page_for_page(page, user_id)
    tracker = _menu_tracker.get(user_id)
    if tracker:
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=tracker["chat_id"],
                message_id=tracker["message_id"],
                parse_mode="Markdown",
                reply_markup=markup,
            )
        except Exception:
            pass  # Menu may have been deleted — user can type /menu again


async def _navigate_page(callback: CallbackQuery, user_id: int, page: str) -> None:
    """Navigate to a specific menu page."""
    _current_page[user_id] = page

    if page == PAGE_MAIN:
        await _show_main_menu(callback, user_id)
        return
    elif page == PAGE_TOOLS:
        text, markup = await build_tools_page()
    elif page == PAGE_CONVERSATIONS:
        text, markup = await build_conversations_page(user_id)
    elif page == PAGE_SETTINGS:
        text, markup = await build_settings_page(user_id)
    elif page == PAGE_ADMIN:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_page(user_id)
    elif page == PAGE_ADMIN_MODEL:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_model_page(user_id)
    elif page == PAGE_ADMIN_FEEDBACK:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_feedback_page(user_id)
    elif page == PAGE_ADMIN_WEBSITE:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_website_page(user_id)
    elif page == PAGE_ADMIN_GROUPLINK:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_grouplink_page(user_id)
    elif page == PAGE_ADMIN_PASSCODE:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_passcode_page(user_id)
    elif page == PAGE_ADMIN_REMOVE_PASSCODE:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_remove_passcode_page(user_id)
    elif page == PAGE_ADMIN_STATS:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_stats_page(user_id)
    elif page == PAGE_ADMIN_USERS:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_users_page(user_id)
    elif page == PAGE_ADMIN_DIAG:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_diag_page(user_id)
    elif page == PAGE_ADMIN_ANALYTICS:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_analytics_page(user_id)
    elif page == PAGE_ADMIN_LLM_CONFIG:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_llm_config_page(user_id)
    elif page == PAGE_ADMIN_SKILLS:
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        text, markup = await build_admin_skills_page(user_id)
    else:
        text, markup = await build_main_page(user_id)

    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        # Update tracker — this is the single pinned menu message
        _menu_tracker[user_id] = {
            "chat_id": callback.message.chat.id,
            "message_id": callback.message.message_id,
        }
    except Exception:
        pass  # Never fallback to answer — single pinned message only
    await callback.answer()


async def _set_pending_action(callback: CallbackQuery, user_id: int, action: str) -> None:
    """Set a pending action and show the input prompt in the pinned menu."""
    _pending_action[user_id] = action
    text, markup = await build_input_prompt(user_id, action)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except Exception:
        pass  # Never fallback to answer — edits the pinned menu only
    await callback.answer()


async def _execute_direct_action(callback: CallbackQuery, user_id: int, action: str) -> None:
    """Execute an action that doesn't need user input and return to menu."""
    if action == "stats":
        await _show_stats(callback, user_id)
        return  # _show_stats calls callback.answer() internally — prevent double-call
    elif action == "new_chat":
        await SessionManager.clear_conversation(user_id)
        bust_cache(user_id)
        await callback.answer()
        await _send_info_message(callback, "🆕 Started a new conversation. Context cleared.")
    elif action == "retry":
        import app.router as _router
        last_text = _router._last_user_message.get(user_id)
        if not last_text:
            await callback.answer("No message to retry.", show_alert=True)
            return
        try:
            await callback.message.edit_text("🔄 Retrying last message...")
        except Exception:
            pass  # Never fallback to answer
        # Simulate a message for processing
        _processing_bot_msg = callback.message
        await _router._process_text_response(_processing_bot_msg, last_text)
        pending = pop_pending_images(user_id)
        for img_path in pending:
            try:
                with open(img_path, "rb") as f:
                    await callback.message.answer_photo(
                        BufferedInputFile(f.read(), filename="image.png"),
                        caption="Generated image",
                    )
                import os
                os.remove(img_path)
            except Exception as e:
                logger.error("Menu retry image send failed: %s", e)
        await _return_to_page(callback.message, user_id)
    elif action == "help":
        help_text = (
            "🤖 *Bud AI Help*\n\n"
            "I'm an AI assistant powered by NEAR AI and GPT-5.2.\n\n"
            "*What I can do:*\n"
            "• Chat with memory across conversations\n"
            "• Search the web for current information\n"
            "• Generate AI images from descriptions\n"
            "• Read and analyze files (PDF, images, code)\n"
            "• Calculate math expressions\n\n"
            "*Commands:*\n"
            "/menu — Open this menu\n"
            "/new — Start a new conversation\n"
            "/retry — Retry the last message\n"
            "/prompt — Set custom system prompt\n"
            "/stats — View your usage stats\n"
            "/export — Export current conversation"
        )
        await callback.answer()
        await _send_info_message(callback, help_text)
    elif action == "export_conv":
        conv = await Database.get_active_conversation(user_id)
        if not conv:
            await callback.answer("No active conversation to export.", show_alert=True)
            return
        export_text = await Database.export_conversation(conv["id"])
        if not export_text:
            await callback.answer("No messages in conversation.", show_alert=True)
            return
        try:
            export_bytes = export_text.encode("utf-8")
            await callback.message.answer_document(
                BufferedInputFile(export_bytes, filename=f"conversation_{conv['id']}.txt"),
                caption=f"Exported: {conv.get('title', 'Untitled')}",
            )
        except Exception as e:
            logger.error("Export failed: %s", e)
            await callback.answer("Failed to export conversation.", show_alert=True)
        await _return_to_page(callback.message, user_id)
    elif action == "admin_stats":
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        await callback.answer()
        try:
            total_users = await Database.get_total_user_count()
        except Exception:
            total_users = 0
        try:
            today_usage = await Database.get_all_users_usage_today()
            total_reqs = sum(u.get("requests", 0) for u in today_usage)
        except Exception:
            total_reqs = 0
        uptime_hours = 0
        try:
            import app.router as _router
            import time
            uptime_hours = int(time.time() - _router._bot_start_time) // 3600
        except Exception:
            pass
        text = (
            f"📊 *Admin Stats*\n\n"
            f"👥 Total Users: `{total_users}`\n"
            f"📊 Requests Today: `{total_reqs}`\n"
            f"⏱️ Uptime: `{uptime_hours}h`\n\n"
            f"Daily cap per user: `{settings.rate_limit_daily_cap}`\n"
            f"Rate limit window: `{settings.rate_limit_window_seconds}s`"
        )
        await _send_info_message(callback, text)
    elif action == "admin_users":
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        await callback.answer()
        import app.router as _router
        active = list(_router._processing)
        if active:
            text = "*Currently Processing:*\n" + "\n".join(f"• `{uid}`" for uid in active[:30])
        else:
            text = "No users currently processing."
        await _send_info_message(callback, text)
    elif action == "admin_clear_feedback":
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        deleted = await Database.clear_feedback()
        await callback.answer(f"Deleted {deleted} feedback entries.")
        # Refresh feedback page
        text2, markup2 = await build_admin_feedback_page(user_id)
        try:
            await callback.message.edit_text(text2, parse_mode="Markdown", reply_markup=markup2)
        except Exception:
            pass

    elif action == "dismiss_info":
        await _dismiss_info(callback, user_id)
        return  # calls callback.answer() and _return_to_page internally

    elif action == "open_website":
        website_url = await Database.get_config("website_url")
        if website_url:
            await callback.answer()
            await _send_info_message(callback, f"🌐 Visit our website: {website_url}")
        else:
            await callback.answer("No website link configured.", show_alert=True)
    elif action == "open_group":
        group_link = await Database.get_config("group_link")
        if group_link:
            await callback.answer()
            await _send_info_message(callback, f"👥 Join our group: {group_link}")
        else:
            await callback.answer("No group link configured.", show_alert=True)
    elif action == "admin_remove_website":
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        await Database.delete_config("website_url")
        await callback.answer("Website link removed.")
        text, markup = await build_admin_website_page(user_id)
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            pass
    elif action == "admin_remove_grouplink":
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        await Database.delete_config("group_link")
        await callback.answer("Group link removed.")
        text, markup = await build_admin_grouplink_page(user_id)
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            pass

    elif action == "admin_export_csv":
        if user_id not in settings.admin_ids:
            await callback.answer("Access denied.", show_alert=True)
            return
        try:
            analytics = await Database.get_usage_analytics(7)
            csv_lines = ["date,requests,tokens_in,tokens_out"]
            for day in analytics.get("daily_breakdown", []):
                csv_lines.append(f"{day['date']},{day['reqs']},{day['t_in']},{day['t_out']}")
            csv_text = "\n".join(csv_lines)
            csv_bytes = csv_text.encode("utf-8")
            await callback.message.answer_document(
                BufferedInputFile(csv_bytes, filename=f"analytics_7d.csv"),
                caption="📊 Analytics export (7 days)",
            )
            await callback.answer("CSV exported!")
        except Exception as e:
            logger.error("Export CSV failed: %s", e)
            await callback.answer("Export failed.", show_alert=True)

    await callback.answer()


async def _send_info_message(target: Message | CallbackQuery, text: str, parse_mode: str = "Markdown") -> None:
    """Send an info message as a new message with a 'Back to Menu' dismiss button.

    Never touches the pinned menu — always sends a brand-new message that
    the user can dismiss cleanly.
    """
    builder = InlineKeyboardBuilder()
    builder.row(_button("⬅️  Back to Menu", "action:dismiss_info"), width=1)
    markup = builder.as_markup()
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode=parse_mode, reply_markup=markup)
    else:
        await target.answer(text, parse_mode=parse_mode, reply_markup=markup)


async def _dismiss_info(callback: CallbackQuery, user_id: int) -> None:
    """Delete the info/dismissable message and return to the pinned menu."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
    await _return_to_page(callback.message, user_id)


async def _show_stats(callback: CallbackQuery, user_id: int) -> None:
    """Show user stats in a new message (not editing the menu) so the menu stays intact."""
    await callback.answer()

    today = await Database.get_user_usage_today(user_id)
    lifetime = await Database.get_user_lifetime_usage(user_id)
    conv = await Database.get_active_conversation(user_id)
    conv_id = conv["id"] if conv else 0
    all_msgs = await Database.get_conversation_messages(conv_id, limit=None) if conv else []

    lines = [
        "*Your Stats:*",
        f"• Requests today: `{today.get('requests', 0)}`",
        f"• Messages this conversation: `{len(all_msgs)}`",
        f"• Images generated: `{today.get('images_generated', 0)}`",
    ]
    if lifetime.get("total_requests"):
        lines.append(f"• All-time requests: `{lifetime['total_requests']}`")
    if lifetime.get("total_tokens_in"):
        lines.append(f"• All-time tokens in: ~`{lifetime['total_tokens_in']:,}`")
    if lifetime.get("total_tokens_out"):
        lines.append(f"• All-time tokens out: ~`{lifetime['total_tokens_out']:,}`")

    text = "\n".join(lines)

    # Send as a new message with a "Back" button that dismisses the stats message
    builder = InlineKeyboardBuilder()
    builder.row(_button("⬅️  Back to Menu", "action:dismiss_info"), width=1)
    try:
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  Process user text input for pending actions
# ═══════════════════════════════════════════════════════════════

async def process_pending_action(message: Message, user_id: int, text: str) -> bool:
    """Process a text message as input for a pending menu action.

    Returns True if the message was consumed by a pending action,
    False if no pending action exists (caller should handle normally).
    """
    action = _pending_action.pop(user_id, "")
    if not action:
        return False

    processing_msg = await message.answer("Processing your request...")

    try:
        if action == "web_search":
            result = await execute_tool("web_search", {"query": text})
            await processing_msg.edit_text(f"🔍 *Search Results*\n\n{result}", parse_mode="Markdown")

        elif action == "generate_image":
            from app.skills.image_gen import current_user_id as img_ctx
            img_ctx.set(user_id)
            result = await execute_tool("generate_image", {"prompt": text})
            await processing_msg.edit_text(f"🎨 {result}", parse_mode="Markdown")
            pending = pop_pending_images(user_id)
            for img_path in pending:
                try:
                    with open(img_path, "rb") as f:
                        await message.answer_photo(
                            BufferedInputFile(f.read(), filename="image.png"),
                            caption="Generated image",
                        )
                    import os
                    os.remove(img_path)
                except Exception as e:
                    logger.error("Menu image send failed: %s", e)

        elif action == "calculate":
            result = await execute_tool("calculate", {"expression": text})
            await processing_msg.edit_text(f"🧮 {result}", parse_mode="Markdown")

        elif action == "set_prompt":
            import app.router as _router
            safe, reason = _router._is_safe_prompt(text)
            if not safe:
                await processing_msg.edit_text(f"❌ Prompt rejected: {reason}")
            else:
                await SessionManager.update_system_prompt(user_id, text)
                await processing_msg.edit_text("✅ System prompt updated!")

        elif action == "admin_broadcast":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                all_users = await Database.get_all_user_ids()
                sent = 0
                failed = 0
                for uid in all_users:
                    try:
                        await message.bot.send_message(
                            uid,
                            f"*📢 Broadcast from admin:*\n\n{text}",
                            parse_mode="Markdown",
                        )
                        sent += 1
                    except Exception as e:
                        failed += 1
                        logger.warning("Broadcast failed for user %d: %s", uid, e)
                    if sent % 10 == 0:
                        await asyncio.sleep(0.5)
                await processing_msg.edit_text(f"📢 Broadcast complete. Sent: `{sent}`, Failed: `{failed}`")

        elif action == "admin_block":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                try:
                    target_id = int(text.strip())
                    await Database.set_user_blocked(target_id, True)
                    await processing_msg.edit_text(f"✅ User `{target_id}` blocked.")
                except ValueError:
                    await processing_msg.edit_text("❌ Invalid user ID.")

        elif action == "admin_unblock":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                try:
                    target_id = int(text.strip())
                    await Database.set_user_blocked(target_id, False)
                    await processing_msg.edit_text(f"✅ User `{target_id}` unblocked.")
                except ValueError:
                    await processing_msg.edit_text("❌ Invalid user ID.")

        elif action == "set_website":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                # Validate URL
                url = text.strip()
                if url.startswith("http://") or url.startswith("https://"):
                    await Database.set_config("website_url", url)
                    await processing_msg.edit_text("✅ Website link set successfully!")
                else:
                    await processing_msg.edit_text(
                        "❌ Invalid URL. Must start with http:// or https://"
                    )

        elif action == "set_grouplink":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                url = text.strip()
                if url.startswith("http://") or url.startswith("https://") or url.startswith("https://t.me/"):
                    await Database.set_config("group_link", url)
                    await processing_msg.edit_text("✅ Group link set successfully!")
                else:
                    await processing_msg.edit_text(
                        "❌ Invalid link. Please provide a valid group invite URL."
                    )

        elif action == "set_passcode":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                parts = text.strip().split(maxsplit=1)
                if len(parts) < 2:
                    await processing_msg.edit_text(
                        "❌ Invalid format. Use: `<group_id> <passcode>`"
                    )
                else:
                    try:
                        group_id = int(parts[0])
                        passcode = parts[1].strip()
                        if not passcode:
                            raise ValueError("Empty passcode")
                        await Database.set_group_passcode(group_id, passcode)
                        await processing_msg.edit_text(
                            f"✅ Passcode set for group `{group_id}`."
                        )
                        logger.info("[user=%d] set_passcode group=%s", user_id, str(group_id))
                    except ValueError:
                        await processing_msg.edit_text(
                            "❌ Invalid format. Use: `<group_id> <passcode>`"
                        )

        elif action == "remove_passcode":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                try:
                    group_id = int(text.strip())
                    await Database.remove_group_passcode(group_id)
                    await processing_msg.edit_text(
                        f"✅ Passcode removed for group `{group_id}`."
                    )
                    logger.info("[user=%d] remove_passcode group=%s", user_id, str(group_id))
                except ValueError:
                    await processing_msg.edit_text(
                        "❌ Invalid group ID. Please send a numeric group ID."
                    )

        elif action == "admin_viewconv":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            else:
                try:
                    target_id = int(text.strip())
                    profile = await Database.get_user_full_profile(target_id)
                    if not profile:
                        await processing_msg.edit_text(
                            f"❌ User `{target_id}` not found in database.",
                            parse_mode="Markdown",
                        )
                    else:
                        username = profile.get("username") or "—"
                        first_name = profile.get("first_name") or "—"
                        blocked = "🚫 Blocked" if profile.get("is_blocked") else "✅ Active"
                        model = profile.get("preferred_model") or "default"
                        sys_prompt = profile.get("system_prompt", "")
                        if len(sys_prompt) > 100:
                            sys_prompt = sys_prompt[:100] + "..."

                        lines = [
                            "━━━━━━━━━━━━━━━━━━━━━━━",
                            f"👤 *User Profile: `{target_id}`*",
                            "━━━━━━━━━━━━━━━━━━━━━━━",
                            f"👤 Name: {first_name} (@{username})",
                            f"📊 Status: {blocked} | Model: `{model}`",
                            f"💬 Conversations: {profile.get('conversation_count', 0)}",
                            f"📝 Messages: {profile.get('total_messages', 0)}",
                            f"🔢 Requests: {profile.get('total_req', 0)}",
                            f"📥 Tokens In: {profile.get('total_in', 0):,}",
                            f"📤 Tokens Out: {profile.get('total_out', 0):,}",
                            f"🖼️ Images: {profile.get('total_img', 0)}",
                            f"⚙️ Prompt: `{sys_prompt}`",
                        ]

                        convs = await Database.get_user_conversations_with_messages(target_id)
                        if convs:
                            lines.append("")
                            lines.append("*Recent Conversations:*")
                            for conv in convs[:5]:
                                title = conv.get("title", "Untitled")
                                msgs = conv.get("msg_count", 0)
                                last = conv.get("last_msg", "")
                                if last and len(last) > 60:
                                    last = last[:60] + "..."
                                lines.append(f"  `{conv['id']}` — *{title}* ({msgs} msgs)")
                                if last:
                                    lines.append(f"       ↳ {last}")

                        await processing_msg.edit_text("\n".join(lines), parse_mode="Markdown")
                except ValueError:
                    await processing_msg.edit_text("❌ Invalid user ID. Must be a number.")

        elif action == "admin_clear_feedback":
            if user_id not in settings.admin_ids:
                await processing_msg.edit_text("❌ Access denied.")
            elif text.strip().lower() == "yes":
                try:
                    deleted = await Database.clear_feedback()
                    await processing_msg.edit_text(f"✅ Deleted {deleted} feedback entries.")
                except Exception as e:
                    await processing_msg.edit_text(f"❌ Failed to clear feedback: {e}")
            else:
                await processing_msg.edit_text("❌ Cancelled. No feedback was deleted.")

        else:
            await processing_msg.edit_text(f"Unknown action: {action}")

    except Exception as e:
        logger.error("Menu action '%s' failed for user %d: %s", action, user_id, e)
        try:
            await processing_msg.edit_text("❌ Action failed. Please try again.")
        except Exception:
            await message.answer("❌ Action failed. Please try again.")

    # Return to previous page after a brief pause
    await asyncio.sleep(1.5)
    await _return_to_page(message, user_id)
    return True


# ═══════════════════════════════════════════════════════════════
#  Handler: /menu command
# ═══════════════════════════════════════════════════════════════

CMD_MENU = Command("menu", "m")

@menu_router.message(CMD_MENU)
async def cmd_menu(message: Message) -> None:
    """Open the premium menu."""
    user_id = message.from_user.id
    _current_page[user_id] = PAGE_MAIN
    await _show_main_menu(message, user_id)


# ═══════════════════════════════════════════════════════════════
#  Handler: Callback queries from menu buttons
# ═══════════════════════════════════════════════════════════════

@menu_router.callback_query(F.data.startswith(f"{MENU_PREFIX}:"))
async def menu_callback_handler(callback: CallbackQuery) -> None:
    """Route all menu callback queries to the correct handler."""
    user_id = callback.from_user.id

    # Parse callback data: menu:<type>:<value>
    parts = callback.data.split(":", 2)
    if len(parts) < 2:
        await callback.answer("Invalid menu action.", show_alert=True)
        return

    cb_type = parts[1]
    cb_value = parts[2] if len(parts) > 2 else ""

    try:
        if cb_type == "page":
            # Navigation: menu:page:<page_name>
            await _navigate_page(callback, user_id, cb_value)

        elif cb_type == "action":
            # Action: menu:action:<action_name>
            if cb_value in ("stats", "new_chat", "retry", "help", "export_conv",
                            "admin_stats", "admin_users", "admin_clear_feedback", "cancel_input",
                            "open_website", "open_group", "admin_remove_website", "admin_remove_grouplink",
                            "admin_export_csv", "dismiss_info"):
                if cb_value == "cancel_input":
                    _pending_action.pop(user_id, None)
                    await _navigate_page(callback, user_id, _current_page.get(user_id, PAGE_MAIN))
                else:
                    await _execute_direct_action(callback, user_id, cb_value)
            else:
                # Requires user input
                await _set_pending_action(callback, user_id, cb_value)

        elif cb_type == "conv_switch":
            # Conversation switch: menu:conv_switch:<conv_id>
            conv_id = int(cb_value)
            ok = await Database.set_active_conversation(user_id, conv_id)
            if ok:
                conv = await Database.get_conversation_by_id(conv_id)
                title = conv.get("title", "Conversation") if conv else "Conversation"
                await callback.answer(f"Switched to: {title}")
            else:
                await callback.answer("Failed to switch conversation.", show_alert=True)
            # Refresh conversations page
            await _navigate_page(callback, user_id, PAGE_CONVERSATIONS)

        elif cb_type == "admin_switch_model":
            # Admin model switch: menu:admin_switch_model:<model_name>
            if user_id not in settings.admin_ids:
                await callback.answer("Access denied.", show_alert=True)
                return
            model_name = cb_value
            if model_name not in settings.available_models:
                await callback.answer("Invalid model.", show_alert=True)
                return
            await Database.set_preferred_model(user_id, model_name)
            await callback.answer(f"Switched to {model_name}")
            # Refresh model page
            await _navigate_page(callback, user_id, PAGE_ADMIN_MODEL)

        else:
            await callback.answer("Unknown menu action.", show_alert=True)

    except Exception as e:
        logger.error("Menu callback error for user %d: %s", user_id, e)
        # Try to recover by showing main menu
        try:
            await _show_main_menu(callback.message, user_id)
        except Exception:
            pass
        await callback.answer("An error occurred. Returning to main menu.", show_alert=True)