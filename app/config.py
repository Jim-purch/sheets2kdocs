"""Application settings loaded from environment variables / .env file."""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Load .env from CWD and from package root's parent (repo root)
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass

logger = logging.getLogger("sheets2kdocs")


def _env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val.strip() if isinstance(val, str) else default


class Settings:
    def __init__(self) -> None:
        self.admin_username: str = _env("ADMIN_USERNAME", "admin")
        self.admin_password: str = _env("ADMIN_PASSWORD")
        if not self.admin_password:
            logger.warning(
                "ADMIN_PASSWORD is empty! Generating a random one for this run. "
                "Set ADMIN_PASSWORD in your .env file."
            )
            self.admin_password = secrets.token_urlsafe(12)

        self.secret_key: str = _env("SECRET_KEY")
        if not self.secret_key:
            self.secret_key = secrets.token_hex(32)
            logger.warning("SECRET_KEY is empty, generated an ephemeral one (sessions will not survive restarts).")

        self.data_dir: Path = Path(_env("DATA_DIR", "./data")).expanduser()
        self.db_path: Path = self.data_dir / "sheets2kdocs.db"
        self.port: int = int(_env("PORT", "8300") or "8300")

        base_url = _env("BASE_URL")
        self.base_url: str = base_url.rstrip("/")

        self.timezone: str = _env("TZ", _env("TIMEZONE", "Asia/Shanghai"))

        # Kdocs open platform credentials
        self.kdocs_app_id: str = _env("KDOCS_APP_ID")
        self.kdocs_app_key: str = _env("KDOCS_APP_KEY")
        custom_redirect = _env("KDOCS_REDIRECT_URI")
        self.kdocs_redirect_uri: str = custom_redirect or (
            f"{self.base_url}/auth/kdocs/callback" if self.base_url else ""
        )

        # Google credentials (optional: for API-key / service-account modes)
        self.google_api_key: str = _env("GOOGLE_API_KEY")
        self.google_sa_json: str = _env("GOOGLE_SERVICE_ACCOUNT_JSON")  # inline JSON or file path


settings = Settings()
