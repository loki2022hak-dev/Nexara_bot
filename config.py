from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "").strip()
    database_url: str = os.getenv("DATABASE_URL", "").strip()
    shodan_api_key: str = os.getenv("SHODAN_API_KEY", "").strip()
    censys_bearer_token: str = os.getenv("CENSYS_BEARER_TOKEN", "").strip()
    vt_api_key: str = os.getenv("VT_API_KEY", "").strip()
    app_env: str = os.getenv("APP_ENV", "prod").strip()
    log_level: str = os.getenv("LOG_LEVEL", "INFO").strip()
    free_daily_limit: int = int(os.getenv("FREE_DAILY_LIMIT", "3"))
    intel_daily_limit: int = int(os.getenv("INTEL_DAILY_LIMIT", "25"))
    agency_daily_limit: int = int(os.getenv("AGENCY_DAILY_LIMIT", "150"))
    warroom_daily_limit: int = int(os.getenv("WARROOM_DAILY_LIMIT", "1000"))

settings = Settings()

if not settings.bot_token:
    raise RuntimeError("BOT_TOKEN missing in .env")
if not settings.database_url:
    raise RuntimeError("DATABASE_URL missing in .env")
