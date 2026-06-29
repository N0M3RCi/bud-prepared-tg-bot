#!/usr/bin/env python3
"""
Telegram AI Bot — NEAR AI + GPT-5.2
Powered by aiogram 3.x with PostgreSQL memory and function-calling skills.

Supports two modes:
  - Webhook (primary/default):   USE_WEBHOOK=1 WEBHOOK_URL=https://example.com python bot.py
  - Polling (fallback):          USE_WEBHOOK=0 python bot.py

Deploy on Railway with:  python bot.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time

import aiohttp.web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

from app.config import settings
from app.database import Database
from app.router import router, set_bot_start_time, set_bot_username
from app.dashboard_reporter import report_system_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Internal health state (single source — router.set_bot_start_time is the setter)
_bot_start_time: float = 0.0


# ─────────────────────────── Background Tasks ───────────────────────────

async def _db_keepalive(interval: int = 30) -> None:
    """Periodically ping the database to keep the connection alive."""
    while True:
        # Add jitter (±25%) to prevent thundering herd on reconnects
        jitter = interval * (0.75 + random.random() * 0.5)
        await asyncio.sleep(jitter)
        try:
            alive = await Database.ping()
            if not alive:
                logger.warning("Database keepalive ping failed — will retry.")
        except Exception as e:
            logger.warning("Database keepalive error: %s", e)


def _clean_directory(now: float, directory: str, max_age: int) -> int:
    """Delete files older than max_age seconds in the given directory. Returns count."""
    count = 0
    if not os.path.isdir(directory):
        return count
    for fname in os.listdir(directory):
        fpath = os.path.join(directory, fname)
        if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age:
            try:
                os.remove(fpath)
                count += 1
            except OSError:
                pass
    return count


async def _cleanup_stale_files(interval: int = 300, max_age: int = 3600) -> None:
    """Periodically delete stale temp files across all temp directories."""
    dirs = ["/tmp/bot_downloads", "/tmp/bot_images"]
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval)
        try:
            now = time.time()
            total = 0
            for d in dirs:
                cleaned = await loop.run_in_executor(None, _clean_directory, now, d, max_age)
                total += cleaned
            if total:
                logger.info("Cleaned %d stale temp files", total)
        except Exception as e:
            logger.warning("File cleanup error: %s", e)


# ─────────────────────────── Lifecycle Hooks ───────────────────────────

async def on_startup(bot: Bot) -> None:
    """Initialize database schema and start background tasks."""
    global _bot_start_time
    _bot_start_time = time.time()
    set_bot_start_time(_bot_start_time)

    # Cache bot username for group mention detection
    try:
        bot_me = await bot.me()
        set_bot_username(bot_me.username or "")
        logger.info("Bot username: @%s", bot_me.username)
    except Exception as e:
        logger.warning("Failed to fetch bot info: %s", e)

    # Register command list with Telegram (overwrites any old commands)
    try:
        # First, completely purge all previously set commands (including BotFather-set)
        await bot.delete_my_commands()

        public_commands = [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="menu", description="Open the interactive menu"),
            BotCommand(command="help", description="Show available commands"),
            BotCommand(command="new", description="Start a new conversation"),
            BotCommand(command="prompt", description="Set system prompt"),
            BotCommand(command="model", description="View or switch AI model"),
            BotCommand(command="settings", description="View your settings"),
            BotCommand(command="stats", description="View usage statistics"),
            BotCommand(command="retry", description="Regenerate last response"),
            BotCommand(command="search", description="Search the web for information"),
            BotCommand(command="imagine", description="Generate an AI image"),
            BotCommand(command="calc", description="Calculate math expressions"),
            BotCommand(command="ping", description="Check bot status"),
            BotCommand(command="chatid", description="Get current chat ID"),
            BotCommand(command="conversations", description="Manage conversations"),
            BotCommand(command="switch", description="Switch conversation"),
            BotCommand(command="rename", description="Rename a conversation"),
            BotCommand(command="delete_conv", description="Delete a conversation"),
            BotCommand(command="export", description="Export conversation"),
            BotCommand(command="feedback", description="Send feedback or suggestions"),
        ]
        await bot.set_my_commands(public_commands)

        # BotFather-style premium admin commands with emojis
        admin_commands = [
            BotCommand(command="menu", description="📋 Open interactive menu"),
            BotCommand(command="admin_stats", description="📊 Bot usage statistics"),
            BotCommand(command="admin_users", description="👥 List all users"),
            BotCommand(command="admin_broadcast", description="📢 Send broadcast message"),
            BotCommand(command="admin_block", description="🚫 Block a user"),
            BotCommand(command="admin_unblock", description="✅ Unblock a user"),
            BotCommand(command="admin_feedback", description="💬 View user feedback"),
            BotCommand(command="admin_clear_feedback", description="🗑️ Clear all feedback"),
            BotCommand(command="admin_viewconv", description="🔍 Inspect user conversations & profile"),
            BotCommand(command="admin_llm_config", description="⚙️ View/set LLM runtime configuration"),
            BotCommand(command="admin_diag", description="🩺 Real-time system diagnostics dashboard"),
            BotCommand(command="admin_analytics", description="📈 Usage analytics with CSV export"),
            BotCommand(command="admin_skills", description="🔧 View registered tool/skill registry"),
        ]
        for admin_id in settings.admin_ids:
            try:
                await bot.set_my_commands(
                    admin_commands,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception as e:
                logger.warning("Failed to set admin commands for chat %d: %s", admin_id, e)

        logger.info("Bot commands registered successfully.")
    except Exception as e:
        logger.warning("Failed to register bot commands: %s", e)

    logger.info("Initializing database schema...")
    try:
        await Database.init_schema()
        logger.info("Database schema initialized successfully.")
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        logger.warning("Set DATABASE_URL in your Railway environment to enable persistence.")

    # Start background tasks common to both modes
    asyncio.ensure_future(_db_keepalive())
    asyncio.ensure_future(_cleanup_stale_files())

    if settings.use_webhook:
        logger.info(
            "Bot ready (webhook mode). Model: %s | Image: %s | Webhook URL: %s%s",
            settings.near_ai_model, settings.near_ai_image_model,
            settings.webhook_url, settings.webhook_path,
        )
    else:
        logger.info(
            "Bot ready (polling fallback). Model: %s | Image: %s | Health port: %d",
            settings.near_ai_model, settings.near_ai_image_model,
            settings.health_port,
        )

    # Report bot startup to dashboard
    asyncio.ensure_future(report_system_event(
        "bot_start",
        details=f"Bot started. Model: {settings.near_ai_model} | Webhook: {settings.use_webhook}",
    ))


async def on_shutdown(bot: Bot) -> None:
    """Clean up resources on shutdown."""
    logger.info("Shutting down...")

    # Report bot shutdown to dashboard
    uptime = int(time.time() - _bot_start_time) if _bot_start_time else 0
    asyncio.ensure_future(report_system_event(
        "bot_stop",
        details=f"Bot shutting down. Uptime: {uptime}s",
    ))

    if settings.use_webhook:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook deleted.")
        except Exception as e:
            logger.warning("Failed to delete webhook: %s", e)

    await Database.close()


# ─────────────────────────── Polling Mode ───────────────────────────

async def _health_check_server() -> None:
    """Minimal HTTP health check server for Railway (polling mode only)."""
    port = settings.health_port

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            uptime = int(time.time() - _bot_start_time)

            # Grace period so Railway's deployment healthcheck doesn't fail
            if uptime < _GRACE_SECONDS:
                body = b'{"status":"starting","uptime":%d}' % uptime
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: %d\r\n"
                    b"Connection: close\r\n\r\n" % (len(body),) + body
                )
                return

            db_ok = await Database.ping()
            status = 200 if db_ok else 503
            body = (
                f'{{"status":"{"ok" if db_ok else "degraded"}","uptime":{uptime}}}'
            ).encode()
            writer.write(
                f"HTTP/1.1 {status} {'OK' if db_ok else 'Service Unavailable'}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n".encode() + body
            )
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = None
    for attempt in range(5):
        try:
            server = await asyncio.start_server(handle_client, host="0.0.0.0", port=port)
            break
        except OSError as e:
            if attempt < 4:
                wait = 1.5 ** attempt
                logger.warning("Health server bind failed (attempt %d/5): %s. Retrying in %.1fs...",
                               attempt + 1, e, wait)
                await asyncio.sleep(wait)
            else:
                logger.error("Health server failed to bind after 5 attempts: %s", e)
                return

    logger.info("Health check server listening on 0.0.0.0:%d", port)
    async with server:
        await server.serve_forever()


async def _run_polling(bot: Bot, dp: Dispatcher) -> None:
    """Run the bot in polling mode with a health server."""
    asyncio.ensure_future(_health_check_server())

    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error("Fatal polling error: %s", e)
        raise
    finally:
        await bot.session.close()
        await Database.close()


# ─────────────────────────── Webhook Mode ───────────────────────────

_GRACE_SECONDS = 30  # allow DB to connect during startup


async def _health_handler(request):
    """Health check endpoint for Railway. Returns 200 during the startup grace
    window so Railway's deployment healthcheck doesn't fail while the DB is
    still establishing its first connection."""
    uptime = int(time.time() - _bot_start_time)
    # During the initial startup window, accept 200 even if DB isn't ready
    if uptime < _GRACE_SECONDS:
        return aiohttp.web.Response(
            status=200,
            body=json.dumps({"status": "starting", "uptime": uptime}),
            content_type="application/json",
        )
    db_ok = await Database.ping()
    status = 200 if db_ok else 503
    body = json.dumps({"status": "ok" if db_ok else "degraded", "uptime": uptime})
    return aiohttp.web.Response(
        status=status,
        body=body,
        content_type="application/json",
    )


async def _run_webhook(bot: Bot, dp: Dispatcher) -> None:
    """Run the bot in webhook mode using aiogram + aiohttp.

    Uses aiogram's SimpleRequestHandler for the webhook POST route and
    registers lifecycle hooks directly on the aiohttp Application.
    """
    webhook_full_url = settings.webhook_url.rstrip("/") + settings.webhook_path

    # Build aiohttp app with health check + webhook handler
    app = aiohttp.web.Application()

    # Health check endpoint
    app.router.add_get("/", _health_handler)

    # Webhook POST route — uses aiogram's official handler which
    # validates secret token, resolves Bot instance, and feeds updates
    # to the Dispatcher. Also registers session cleanup on shutdown.
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret_token or None,
    )
    webhook_handler.register(app, path=settings.webhook_path)

    # Register lifecycle hooks directly on the aiohttp app so they fire
    # when the server starts/stops (avoids conflict with aiogram's own
    # lifecycle signals and the "frozen router" issue).
    app.on_startup.append(lambda _: on_startup(bot))
    app.on_shutdown.append(lambda _: on_shutdown(bot))

    # Start the server
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host="0.0.0.0", port=settings.webhook_port)
    await site.start()
    logger.info("Server listening on 0.0.0.0:%d/", settings.webhook_port)

    # Register webhook with Telegram
    logger.info("Registering webhook: %s", webhook_full_url)
    try:
        kwargs: dict[str, str] = {}
        if settings.webhook_secret_token:
            kwargs["secret_token"] = settings.webhook_secret_token
        await bot.set_webhook(
            url=webhook_full_url,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,
            **kwargs,
        )
        webhook_info = await bot.get_webhook_info()
        logger.info(
            "Webhook registered. URL: %s | Pending: %d | Max connections: %d",
            webhook_info.url, webhook_info.pending_update_count,
            webhook_info.max_connections,
        )
    except Exception as e:
        logger.error("Webhook registration failed: %s", e)
        raise

    logger.info(
        "All systems ready. Listening on 0.0.0.0:%d%s",
        settings.webhook_port, settings.webhook_path,
    )

    # Keep alive forever
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


# ─────────────────────────── Main ───────────────────────────

async def main() -> None:
    # Validate required config
    missing = []
    if not settings.bot_token:
        missing.append("BOT_TOKEN")
    if not settings.near_ai_api_key:
        missing.append("NEAR_AI_API_KEY")

    if missing:
        logger.error(
            "Missing required environment variables: %s\n"
            "Copy .env.example to .env and fill in your values.",
            ", ".join(missing),
        )
        sys.exit(1)

    # Webhook mode — primary. Falls back to polling if WEBHOOK_URL is missing.
    if settings.use_webhook:
        if not settings.webhook_url:
            logger.warning(
                "Webhook mode is on but WEBHOOK_URL is not set. "
                "Falling back to polling mode. "
                "Set WEBHOOK_URL to your public HTTPS URL to use webhooks "
                "(e.g., WEBHOOK_URL=https://your-app.com)."
            )
            settings.use_webhook = False
        elif not settings.webhook_url.startswith("https://"):
            logger.warning(
                "WEBHOOK_URL should use HTTPS for production. "
                "Telegram requires HTTPS webhooks."
            )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher()

    # Lifecycle hooks are registered directly on the aiohttp Application
    # in _run_webhook (webhook mode) or on the Dispatcher in _run_polling.
    dp.include_router(router)

    if settings.use_webhook:
        await _run_webhook(bot, dp)
    else:
        # Polling mode registers lifecycle on the Dispatcher
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        await _run_polling(bot, dp)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical("Unhandled exception: %s", e)
        sys.exit(1)