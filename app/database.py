import asyncpg
import logging
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)


class Database:
    _pool: Optional[asyncpg.Pool] = None

    @classmethod
    async def connect(cls) -> asyncpg.Pool:
        if cls._pool is None:
            cls._pool = await asyncpg.create_pool(
                settings.database_url,
                min_size=2,
                max_size=10,
                command_timeout=30,
                server_settings={
                    "application_name": "telegram_ai_bot",
                    "idle_in_transaction_session_timeout": "30000",
                },
            )
        return cls._pool

    @classmethod
    async def close(cls) -> None:
        if cls._pool:
            await cls._pool.close()
            cls._pool = None

    @classmethod
    async def get_pool(cls) -> asyncpg.Pool:
        if cls._pool is None:
            return await cls.connect()
        return cls._pool

    # --- Schema setup ---

    @classmethod
    async def init_schema(cls) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            # Add tool_calls_json column if missing (schema migration)
            await conn.execute("""
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_calls_json TEXT
            """)
            await conn.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_model TEXT
            """)
            await conn.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_done BOOLEAN DEFAULT FALSE
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    system_prompt TEXT DEFAULT 'You are a helpful AI assistant.',
                    max_context_messages INT DEFAULT 50,
                    max_context_tokens INT DEFAULT 128000,
                    is_blocked BOOLEAN DEFAULT FALSE,
                    language_code TEXT DEFAULT 'en',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    title TEXT DEFAULT 'New conversation',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
                    content TEXT NOT NULL,
                    tool_call_id TEXT,
                    tool_name TEXT,
                    token_count INT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS usage_stats (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    date DATE NOT NULL DEFAULT CURRENT_DATE,
                    requests INT DEFAULT 0,
                    tokens_in INT DEFAULT 0,
                    tokens_out INT DEFAULT 0,
                    images_generated INT DEFAULT 0,
                    searches_performed INT DEFAULT 0,
                    UNIQUE(user_id, date)
                );

                CREATE TABLE IF NOT EXISTS rate_limits (
                    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    window_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    request_count INT DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_summaries_conv ON conversation_summaries(conversation_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_user_date ON usage_stats(user_id, date);
                CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at DESC);

                CREATE TABLE IF NOT EXISTS bot_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS group_passcodes (
                    group_id BIGINT PRIMARY KEY,
                    passcode TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT TRUE
                );
            """)

    # --- Users ---

    @classmethod
    async def get_or_create_user(
        cls,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> dict:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", user_id
            )
            if row is None:
                lang = language_code or "en"
                await conn.execute(
                    """INSERT INTO users (user_id, username, first_name, last_name, language_code)
                       VALUES ($1, $2, $3, $4, $5)""",
                    user_id, username, first_name, last_name, lang,
                )
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE user_id = $1", user_id
                )
            return dict(row)

    @classmethod
    async def get_preferred_model(cls, user_id: int) -> Optional[str]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT preferred_model FROM users WHERE user_id = $1", user_id,
            )
            return val

    @classmethod
    async def set_preferred_model(cls, user_id: int, model: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET preferred_model = $1, updated_at = NOW() WHERE user_id = $2",
                model, user_id,
            )

    @classmethod
    async def update_user_prompt(cls, user_id: int, system_prompt: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE users SET system_prompt = $1, updated_at = NOW()
                   WHERE user_id = $2""",
                system_prompt, user_id,
            )

    @classmethod
    async def get_user_settings(cls, user_id: int) -> Optional[dict]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", user_id
            )
            return dict(row) if row else None

    @classmethod
    async def is_user_blocked(cls, user_id: int) -> bool:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT is_blocked FROM users WHERE user_id = $1", user_id
            )
            return row is True

    @classmethod
    async def set_user_blocked(cls, user_id: int, blocked: bool) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_blocked = $1, updated_at = NOW() WHERE user_id = $2",
                blocked, user_id,
            )

    @classmethod
    async def get_all_user_ids(cls) -> list[int]:
        """Get all user IDs for broadcasting."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_blocked = FALSE")
            return [r["user_id"] for r in rows]

    @classmethod
    async def get_total_user_count(cls) -> int:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users")

    # --- Onboarding ---

    @classmethod
    async def is_onboarding_done(cls, user_id: int) -> bool:
        """Check if a user has completed onboarding."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT onboarding_done FROM users WHERE user_id = $1", user_id,
            )
            return val is True

    @classmethod
    async def mark_onboarding_done(cls, user_id: int) -> None:
        """Mark a user's onboarding as complete."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET onboarding_done = TRUE, updated_at = NOW() WHERE user_id = $1",
                user_id,
            )

    # --- Conversations ---

    @classmethod
    async def create_conversation(cls, user_id: int, title: str = "New conversation") -> int:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO conversations (user_id, title)
                   VALUES ($1, $2) RETURNING id""",
                user_id, title,
            )
            return row["id"]

    @classmethod
    async def get_active_conversation(cls, user_id: int) -> Optional[dict]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM conversations
                   WHERE user_id = $1
                   ORDER BY updated_at DESC
                   LIMIT 1""",
                user_id,
            )
            return dict(row) if row else None

    @classmethod
    async def list_conversations(cls, user_id: int) -> list[dict]:
        """List all conversations for a user, ordered by most recent."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.*,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count
                   FROM conversations c
                   WHERE c.user_id = $1
                   ORDER BY c.updated_at DESC
                   LIMIT 50""",
                user_id,
            )
            return [dict(r) for r in rows]

    @classmethod
    async def get_conversation_by_id(cls, conversation_id: int) -> Optional[dict]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM conversations WHERE id = $1", conversation_id,
            )
            return dict(row) if row else None

    @classmethod
    async def update_conversation_title(cls, conversation_id: int, title: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET title = $1, updated_at = NOW() WHERE id = $2",
                title, conversation_id,
            )

    @classmethod
    async def set_active_conversation(cls, user_id: int, conversation_id: int) -> bool:
        """Set a conversation as active by bumping its updated_at."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            # Verify the conversation belongs to this user
            conv = await conn.fetchrow(
                "SELECT id FROM conversations WHERE id = $1 AND user_id = $2",
                conversation_id, user_id,
            )
            if not conv:
                return False
            await conn.execute(
                "UPDATE conversations SET updated_at = NOW() WHERE id = $1",
                conversation_id,
            )
            return True

    # --- Messages ---

    @classmethod
    async def add_message(
        cls,
        conversation_id: int,
        user_id: int,
        role: str,
        content: str,
        tool_call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        token_count: int = 0,
        tool_calls_json: Optional[str] = None,
    ) -> int:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO messages
                       (conversation_id, user_id, role, content, tool_call_id, tool_name, token_count, tool_calls_json)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   RETURNING id""",
                conversation_id, user_id, role, content, tool_call_id, tool_name, token_count, tool_calls_json,
            )
            await conn.execute(
                "UPDATE conversations SET updated_at = NOW() WHERE id = $1",
                conversation_id,
            )
            return row["id"]

    @classmethod
    async def get_conversation_messages(
        cls,
        conversation_id: int,
        limit: Optional[int] = None,
        token_budget: Optional[int] = None,
    ) -> list[dict]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            # Use a subquery to get the most recent messages ordered by created_at DESC,
            # then reverse them so they come out in chronological order
            if limit or token_budget:
                if limit:
                    rows = await conn.fetch(
                        """SELECT * FROM (
                            SELECT * FROM messages
                            WHERE conversation_id = $1
                            ORDER BY created_at DESC
                            LIMIT $2
                        ) sub ORDER BY created_at ASC""",
                        conversation_id, limit,
                    )
                else:
                    rows = await conn.fetch(
                        """SELECT * FROM messages
                           WHERE conversation_id = $1
                           ORDER BY created_at ASC""",
                        conversation_id,
                    )
            else:
                rows = await conn.fetch(
                    """SELECT * FROM messages
                       WHERE conversation_id = $1
                       ORDER BY created_at ASC""",
                    conversation_id,
                )

            messages = [dict(r) for r in rows]

            # Token budget trimming: drop oldest messages until under budget
            if token_budget and messages:
                total_tokens = sum(m.get("token_count", 0) for m in messages)
                while total_tokens > token_budget and len(messages) > 1:
                    removed = messages.pop(0)
                    total_tokens -= removed.get("token_count", 0)

            return messages

    @classmethod
    async def delete_conversation(cls, conversation_id: int) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            # ON DELETE CASCADE handles child tables, but explicit is safer
            await conn.execute(
                "DELETE FROM conversation_summaries WHERE conversation_id = $1",
                conversation_id,
            )
            await conn.execute(
                "DELETE FROM messages WHERE conversation_id = $1",
                conversation_id,
            )
            await conn.execute(
                "DELETE FROM conversations WHERE id = $1",
                conversation_id,
            )

    @classmethod
    async def export_conversation(cls, conversation_id: int) -> Optional[str]:
        """Export conversation as formatted text."""
        messages = await cls.get_conversation_messages(conversation_id, limit=None)
        if not messages:
            return None
        lines = []
        for m in messages:
            role = m["role"].upper()
            content = m["content"]
            lines.append(f"[{role}]: {content}")
        return "\n\n".join(lines)

    # --- Conversation Summaries ---

    @classmethod
    async def save_conversation_summary(cls, conversation_id: int, summary: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, summary) VALUES ($1, $2)",
                conversation_id, summary,
            )

    @classmethod
    async def get_latest_summary(cls, conversation_id: int) -> Optional[str]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT summary FROM conversation_summaries
                   WHERE conversation_id = $1
                   ORDER BY created_at DESC
                   LIMIT 1""",
                conversation_id,
            )
            return row["summary"] if row else None

    # --- Usage Stats ---

    @classmethod
    async def record_usage(
        cls,
        user_id: int,
        tokens_in: int = 0,
        tokens_out: int = 0,
        images: int = 0,
        searches: int = 0,
    ) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO usage_stats (user_id, date, requests, tokens_in, tokens_out, images_generated, searches_performed)
                   VALUES ($1, CURRENT_DATE, 1, $2, $3, $4, $5)
                   ON CONFLICT (user_id, date)
                   DO UPDATE SET
                       requests = usage_stats.requests + 1,
                       tokens_in = usage_stats.tokens_in + $2,
                       tokens_out = usage_stats.tokens_out + $3,
                       images_generated = usage_stats.images_generated + $4,
                       searches_performed = usage_stats.searches_performed + $5""",
                user_id, tokens_in, tokens_out, images, searches,
            )

    @classmethod
    async def get_user_usage_today(cls, user_id: int) -> dict:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT COALESCE(requests, 0) AS requests,
                          COALESCE(tokens_in, 0) AS tokens_in,
                          COALESCE(tokens_out, 0) AS tokens_out,
                          COALESCE(images_generated, 0) AS images_generated
                   FROM usage_stats
                   WHERE user_id = $1 AND date = CURRENT_DATE""",
                user_id,
            )
            if row:
                return dict(row)
            return {"requests": 0, "tokens_in": 0, "tokens_out": 0, "images_generated": 0}

    @classmethod
    async def get_all_users_usage_today(cls) -> list[dict]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT user_id, requests, tokens_in, tokens_out, images_generated
                   FROM usage_stats
                   WHERE date = CURRENT_DATE
                   ORDER BY requests DESC
                   LIMIT 50"""
            )
            return [dict(r) for r in rows]

    @classmethod
    async def get_user_lifetime_usage(cls, user_id: int) -> dict:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT COALESCE(SUM(requests), 0) AS total_requests,
                          COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
                          COALESCE(SUM(tokens_out), 0) AS total_tokens_out,
                          COALESCE(SUM(images_generated), 0) AS total_images
                   FROM usage_stats
                   WHERE user_id = $1""",
                user_id,
            )
            return dict(row) if row else {
                "total_requests": 0, "total_tokens_in": 0,
                "total_tokens_out": 0, "total_images": 0,
            }

    # --- Rate Limiting ---

    @classmethod
    async def check_rate_limit(cls, user_id: int) -> bool:
        """Check if user is within rate limit. Returns True if allowed."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT window_start, request_count FROM rate_limits WHERE user_id = $1",
                user_id,
            )
            window_sec = settings.rate_limit_window_seconds

            if row is None:
                # First request — create entry
                await conn.execute(
                    "INSERT INTO rate_limits (user_id, window_start, request_count) VALUES ($1, NOW(), 1)",
                    user_id,
                )
                return True

            window_start = row["window_start"]
            count = row["request_count"]

            # Check if window has expired
            expired = await conn.fetchval(
                "SELECT $1 < NOW() - interval '1 second' * $2",
                window_start, window_sec,
            )
            if expired:
                # Reset window
                await conn.execute(
                    "UPDATE rate_limits SET window_start = NOW(), request_count = 1 WHERE user_id = $1",
                    user_id,
                )
                return True

            if count >= settings.rate_limit_max_requests:
                return False

            await conn.execute(
                "UPDATE rate_limits SET request_count = request_count + 1 WHERE user_id = $1",
                user_id,
            )
            return True

    # --- Feedback ---

    @classmethod
    async def save_feedback(cls, user_id: int, message: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO feedback (user_id, message) VALUES ($1, $2)",
                user_id, message,
            )

    @classmethod
    async def get_all_feedback(cls, limit: int = 50) -> list[dict]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT f.*, u.username, u.first_name
                   FROM feedback f
                   LEFT JOIN users u ON f.user_id = u.user_id
                   ORDER BY f.created_at DESC
                   LIMIT $1""",
                limit,
            )
            return [dict(r) for r in rows]

    @classmethod
    async def clear_feedback(cls) -> int:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM feedback")
            # asyncpg returns "DELETE N" string
            return int(result.split()[-1])

    # --- Bot Config (website link, group link, etc.) ---

    @classmethod
    async def get_config(cls, key: str) -> Optional[str]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT value FROM bot_config WHERE key = $1", key,
            )
            return val

    @classmethod
    async def set_config(cls, key: str, value: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO bot_config (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value = $2""",
                key, value,
            )

    @classmethod
    async def delete_config(cls, key: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bot_config WHERE key = $1", key)

    # --- Admin Analytics ---

    @classmethod
    async def get_user_full_profile(cls, user_id: int) -> Optional[dict]:
        """Get all user data including conversation and usage summaries."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", user_id,
            )
            if not user:
                return None
            user = dict(user)

            conv_count = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE user_id = $1", user_id,
            )
            user["conversation_count"] = conv_count

            total_msgs = await conn.fetchval(
                """SELECT COUNT(*) FROM messages m
                   JOIN conversations c ON m.conversation_id = c.id
                   WHERE c.user_id = $1""",
                user_id,
            )
            user["total_messages"] = total_msgs

            usage_rows = await conn.fetch(
                """SELECT COALESCE(SUM(requests), 0) AS total_req,
                          COALESCE(SUM(tokens_in), 0) AS total_in,
                          COALESCE(SUM(tokens_out), 0) AS total_out,
                          COALESCE(SUM(images_generated), 0) AS total_img
                   FROM usage_stats WHERE user_id = $1""",
                user_id,
            )
            if usage_rows:
                user.update(dict(usage_rows[0]))

            return user

    @classmethod
    async def get_user_conversations_with_messages(cls, user_id: int) -> list[dict]:
        """Get all conversations for a user with message previews."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.*,
                          (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count,
                          (SELECT content FROM messages m WHERE m.conversation_id = c.id ORDER BY m.created_at DESC LIMIT 1) AS last_msg
                   FROM conversations c
                   WHERE c.user_id = $1
                   ORDER BY c.updated_at DESC
                   LIMIT 20""",
                user_id,
            )
            return [dict(r) for r in rows]

    @classmethod
    async def get_usage_analytics(cls, days: int = 7) -> dict:
        """Get aggregated usage analytics over the specified number of days."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            total_reqs = await conn.fetchval(
                """SELECT COALESCE(SUM(requests), 0) FROM usage_stats
                   WHERE date >= CURRENT_DATE - $1::integer""",
                days,
            )
            total_in = await conn.fetchval(
                """SELECT COALESCE(SUM(tokens_in), 0) FROM usage_stats
                   WHERE date >= CURRENT_DATE - $1::integer""",
                days,
            )
            total_out = await conn.fetchval(
                """SELECT COALESCE(SUM(tokens_out), 0) FROM usage_stats
                   WHERE date >= CURRENT_DATE - $1::integer""",
                days,
            )
            total_images = await conn.fetchval(
                """SELECT COALESCE(SUM(images_generated), 0) FROM usage_stats
                   WHERE date >= CURRENT_DATE - $1::integer""",
                days,
            )
            active_users = await conn.fetchval(
                """SELECT COUNT(DISTINCT user_id) FROM usage_stats
                   WHERE date >= CURRENT_DATE - $1::integer""",
                days,
            )
            top_users = await conn.fetch(
                """SELECT u.user_id, u.username, COALESCE(SUM(s.requests), 0) AS reqs
                   FROM usage_stats s
                   JOIN users u ON s.user_id = u.user_id
                   WHERE s.date >= CURRENT_DATE - $1::integer
                   GROUP BY u.user_id, u.username
                   ORDER BY reqs DESC
                   LIMIT 10""",
                days,
            )
            daily_breakdown = await conn.fetch(
                """SELECT date, COALESCE(SUM(requests), 0) AS reqs,
                          COALESCE(SUM(tokens_in), 0) AS t_in,
                          COALESCE(SUM(tokens_out), 0) AS t_out
                   FROM usage_stats
                   WHERE date >= CURRENT_DATE - $1::integer
                   GROUP BY date
                   ORDER BY date ASC""",
                days,
            )
            return {
                "total_requests": total_reqs or 0,
                "tokens_in": total_in or 0,
                "tokens_out": total_out or 0,
                "images_generated": total_images or 0,
                "active_users": active_users or 0,
                "top_users": [dict(r) for r in top_users],
                "daily_breakdown": [dict(r) for r in daily_breakdown],
            }

    @classmethod
    async def get_db_pool_stats(cls) -> dict:
        """Get database connection pool statistics."""
        if cls._pool is None:
            return {"status": "disconnected", "pool_size": 0, "free": 0}
        sizes = cls._pool._holders
        free = sum(1 for h in sizes if not h._in_use)
        return {
            "status": "connected",
            "pool_size": len(sizes),
            "free": free,
            "min_size": cls._pool._minsize,
            "max_size": cls._pool._maxsize,
        }

    @classmethod
    async def get_total_messages_count(cls) -> int:
        """Get total messages across all conversations."""
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM messages")

    # --- Group Passcodes ---

    @classmethod
    async def set_group_passcode(cls, group_id: int, passcode: str) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO group_passcodes (group_id, passcode, enabled)
                   VALUES ($1, $2, TRUE)
                   ON CONFLICT (group_id) DO UPDATE SET passcode = $2, enabled = TRUE""",
                group_id, passcode,
            )

    @classmethod
    async def remove_group_passcode(cls, group_id: int) -> None:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM group_passcodes WHERE group_id = $1", group_id,
            )

    @classmethod
    async def get_group_passcode(cls, group_id: int) -> Optional[str]:
        pool = await cls.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT passcode FROM group_passcodes WHERE group_id = $1 AND enabled = TRUE",
                group_id,
            )
            return row["passcode"] if row else None

    # --- Health / Keepalive ---

    @classmethod
    async def ping(cls) -> bool:
        """Check if the database connection is alive."""
        try:
            pool = await cls.get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception as e:
            logger.warning("Database ping failed: %s", e)
            return False