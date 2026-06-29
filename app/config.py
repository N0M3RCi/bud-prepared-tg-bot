import os
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _parse_admin_ids(raw: str) -> list[int]:
    """Safely parse comma-separated admin IDs, skipping invalid entries."""
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("Invalid admin ID in ADMIN_IDS: '%s' — skipping", part)
    return ids


@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))

    # NEAR AI / LLM
    near_ai_api_key: str = field(default_factory=lambda: os.getenv("NEAR_AI_API_KEY", ""))
    near_ai_base_url: str = field(default_factory=lambda: os.getenv("NEAR_AI_BASE_URL", "https://cloud-api.near.ai/v1"))
    near_ai_model: str = field(default_factory=lambda: os.getenv("NEAR_AI_MODEL", "openai/gpt-5.2"))
    near_ai_image_model: str = field(default_factory=lambda: os.getenv("NEAR_AI_IMAGE_MODEL", "dall-e-3"))
    near_ai_image_size: str = field(default_factory=lambda: os.getenv("NEAR_AI_IMAGE_SIZE", "1024x1024"))
    near_ai_image_base_url: str = field(default_factory=lambda: os.getenv("NEAR_AI_IMAGE_BASE_URL", ""))
    near_ai_image_api_key: str = field(default_factory=lambda: os.getenv("NEAR_AI_IMAGE_API_KEY", ""))

    # Database
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))

    # Web Search
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))

    # Admin
    admin_ids: list[int] = field(default_factory=lambda: _parse_admin_ids(os.getenv("ADMIN_IDS", "")))

    # Conversation settings
    max_messages_per_conversation: int = int(os.getenv("MAX_MESSAGES_PER_CONVERSATION", "50"))
    max_tokens_per_conversation: int = int(os.getenv("MAX_TOKENS_PER_CONVERSATION", "128000"))
    max_response_tokens: int = int(os.getenv("MAX_RESPONSE_TOKENS", "4096"))

    # File reader
    file_reader_max_chars: int = int(os.getenv("FILE_READER_MAX_CHARS", "15000"))
    file_max_size_mb: int = int(os.getenv("FILE_MAX_SIZE_MB", "20"))
    blocked_file_extensions: list[str] = field(default_factory=lambda: [
        ext.strip().lower()
        for ext in os.getenv("BLOCKED_FILE_EXTENSIONS", ".exe,.bat,.sh,.com,.dll,.msi,.scr,.pif,.vbs,.ps1,.jar,.app,.cmd,.reg,.sys,.bin,.elf").split(",")
        if ext.strip()
    ])

    # Rate limiting
    rate_limit_max_requests: int = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "20"))
    rate_limit_window_seconds: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    rate_limit_daily_cap: int = int(os.getenv("RATE_LIMIT_DAILY_CAP", "200"))

    # Health / server — respects Railway's $PORT env var as fallback
    health_port: int = field(default_factory=lambda: int(os.getenv("HEALTH_PORT") or os.getenv("PORT", "8080")))

    # Smart model routing — fast model for simple queries
    fast_model: str = field(default_factory=lambda: os.getenv("FAST_MODEL", "openai/gpt-4o"))

    # Streaming — progressive: fast initial updates, then settle
    stream_edit_interval: float = float(os.getenv("STREAM_EDIT_INTERVAL", "1.5"))
    stream_initial_interval: float = float(os.getenv("STREAM_INITIAL_INTERVAL", "0.35"))
    stream_initial_updates: int = int(os.getenv("STREAM_INITIAL_UPDATES", "3"))

    # Token counting
    token_estimation_chars_per_token: int = int(os.getenv("TOKEN_ESTIMATION_CHARS_PER_TOKEN", "4"))

    # Prompt injection protection
    max_system_prompt_length: int = int(os.getenv("MAX_SYSTEM_PROMPT_LENGTH", "2000"))

    # Dashboard URL for event reporting
    dashboard_url: str = field(default_factory=lambda: os.getenv("DASHBOARD_URL", ""))

    # Webhook mode (primary — defaults to True; set USE_WEBHOOK=0 for polling)
    use_webhook: bool = os.getenv("USE_WEBHOOK", "1").lower() not in ("0", "false", "no")
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))
    webhook_path: str = field(default_factory=lambda: os.getenv("WEBHOOK_PATH", "/webhook"))
    webhook_port: int = field(default_factory=lambda: int(os.getenv("WEBHOOK_PORT") or os.getenv("PORT", "8080")))
    # Secret token Telegram will send in X-Telegram-Bot-Api-Secret-Token header for verification
    webhook_secret_token: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET_TOKEN", ""))

    # Available models for switching
    available_models: list[str] = field(default_factory=lambda: [
        m.strip()
        for m in os.getenv("AVAILABLE_MODELS", "openai/gpt-5.2,openai/gpt-4o,openai/o3-mini").split(",")
        if m.strip()
    ])


settings = Settings()