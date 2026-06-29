"""
Onboarding flow for new users.

When a new user sends /start for the first time, they see a 3-button menu:
  1. 👥 Join Telegram Group   → opens group_invite_link
  2. 🌐 Use Web Version        → opens website_url
  3. 💬 Continue in DM         → marks onboarding done, shows welcome

The menu persists until the user picks option 3. Any text typed while the menu
is showing re-prompts them (handled in router.py's handle_message).
"""

from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.database import Database

logger = logging.getLogger(__name__)

onboarding_router = Router()

# Callback data prefix
ONBOARDING_PREFIX = "onb"


async def build_onboarding_page() -> tuple[str, object]:
    """Build the onboarding menu with up to 3 buttons.

    Buttons 1 and 2 are only shown if the admin has configured the respective
    links via the Admin Menu → Website / Group settings.
    """
    website_url = await Database.get_config("website_url")
    group_link = await Database.get_config("group_link")

    builder = InlineKeyboardBuilder()

    if group_link:
        builder.row(
            InlineKeyboardButton(text="👥 Join Telegram Group", url=group_link),
            width=1,
        )
    if website_url:
        builder.row(
            InlineKeyboardButton(text="🌐 Use Web Version", url=website_url),
            width=1,
        )
    builder.row(
        InlineKeyboardButton(
            text="💬 Continue in DM",
            callback_data=f"{ONBOARDING_PREFIX}:dm",
        ),
        width=1,
    )

    text = (
        "*👋 Welcome to Bud AI Bot!*\n\n"
        "How would you like to use me?"
    )

    return text, builder.as_markup()


async def _show_onboarding(message: Message) -> None:
    """Send the onboarding menu to the user."""
    text, markup = await build_onboarding_page()
    await message.answer(text, parse_mode="Markdown", reply_markup=markup)


# ═══════════════════════════════════════════════════════════════
#  Handlers
# ═══════════════════════════════════════════════════════════════


@onboarding_router.callback_query(F.data == f"{ONBOARDING_PREFIX}:dm")
async def callback_onboarding_dm(callback: CallbackQuery) -> None:
    """Handle 'Continue in DM' — mark onboarding done and show welcome."""
    user_id = callback.from_user.id
    await Database.mark_onboarding_done(user_id)
    logger.info("[user=%d] onboarding_complete", user_id)

    # Replace the onboarding menu with the welcome text
    await callback.message.edit_text(
        "✅ Onboarding complete! You can now chat with me directly.\n\n"
        "Hello! I'm an AI assistant powered by NEAR AI and GPT-5.2.\n\n"
        "I can:\n"
        "• Chat with memory\n"
        "• Search the web\n"
        "• Generate images\n"
        "• Read and analyze files (PDFs, images, code)\n"
        "• Calculate math expressions\n\n"
        "Type /menu to open the interactive menu, or just start chatting!",
        parse_mode="Markdown",
    )
    await callback.answer()