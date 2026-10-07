"""Central configuration for the Vijay Vargiya Group of Hospitals platform."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _load_dotenv() -> None:
    """Tiny .env loader (no external dependency required)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    app_name: str = os.getenv("APP_NAME", "Vijay Vargiya Group of Hospitals")
    app_env: str = os.getenv("APP_ENV", "development")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)

    # --- database ---
    # The connection can be configured EITHER way:
    #   * DATABASE_URL=mysql+pymysql://user:password@host:port/dbname
    #   * DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD
    # DB_* wins and DATABASE_URL fills the blanks (see the properties below), so
    # the two can never disagree. This matters because helpers such as
    # tests/mysql_checks.py and app/tools/* use settings.db_password directly -
    # an empty DB_PASSWORD used to break them even though the app itself could
    # still connect through DATABASE_URL.
    _db_host_env: str = os.getenv("DB_HOST", "")
    _db_port_env: str = os.getenv("DB_PORT", "")
    _db_name_env: str = os.getenv("DB_NAME", "")
    _db_user_env: str = os.getenv("DB_USER", "")
    _db_password_env: str = os.getenv("DB_PASSWORD", "")
    database_url_override: str = os.getenv("DATABASE_URL", "").strip()
    # MySQL is mandatory for this project. There is intentionally no SQLite fallback.

    @property
    def _url_parts(self) -> dict[str, str]:
        """Host/port/user/password/database parsed out of DATABASE_URL."""
        from urllib.parse import unquote, urlparse

        url = self.database_url_override
        if not url:
            return {}
        try:
            parsed = urlparse(url)
        except ValueError:
            return {}
        if not parsed.hostname and not parsed.username:
            return {}
        return {
            "host": parsed.hostname or "",
            "port": str(parsed.port or ""),
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "name": (parsed.path or "").lstrip("/"),
        }

    @property
    def db_host(self) -> str:
        return self._db_host_env or self._url_parts.get("host") or "127.0.0.1"

    @property
    def db_port(self) -> int:
        raw = self._db_port_env or self._url_parts.get("port") or ""
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 3306

    @property
    def db_name(self) -> str:
        return self._db_name_env or self._url_parts.get("name") or "vijay_vargiya_hospital"

    @property
    def db_user(self) -> str:
        return self._db_user_env or self._url_parts.get("user") or "vvh_app"

    @property
    def db_password(self) -> str:
        # DB_PASSWORD is used verbatim (never silently rewritten - a real
        # password may legitimately contain a % sign). When it is blank the
        # password is taken from DATABASE_URL, where it IS percent-escaped, so
        # "Vvh%402026Secure" correctly becomes "Vvh@2026Secure".
        return self._db_password_env or self._url_parts.get("password") or ""

    def password_looks_url_encoded(self) -> bool:
        """True when DB_PASSWORD looks like it was copied from a URL.

        DB_PASSWORD wants the RAW password (Vvh@2026Secure); DATABASE_URL wants
        the %-escaped form (Vvh%402026Secure). Pasting the URL form into
        DB_PASSWORD sends a literal "%40" to MySQL, so the app warns instead of
        guessing - changing a credential behind the operator's back would be
        worse than a clear message.
        """
        import re

        return bool(re.search(r"%[0-9A-Fa-f]{2}", self._db_password_env or ""))

    # --- security ---
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    access_token_minutes: int = _int("ACCESS_TOKEN_MINUTES", 60)
    refresh_token_days: int = _int("REFRESH_TOKEN_DAYS", 14)
    password_hash_iterations: int = _int("PASSWORD_HASH_ITERATIONS", 260_000)
    max_login_attempts: int = _int("MAX_LOGIN_ATTEMPTS", 5)
    lockout_minutes: int = _int("LOCKOUT_MINUTES", 15)

    # --- storage ---
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage"))).resolve()
    max_upload_mb: int = _int("MAX_UPLOAD_MB", 25)

    # --- messaging ---
    whatsapp_enabled: bool = _bool("WHATSAPP_ENABLED", False)
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_whatsapp_from: str = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    email_enabled: bool = _bool("EMAIL_ENABLED", False)
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    email_from: str = os.getenv("EMAIL_FROM", "no-reply@vijayvargiiyahospital.in")

    # --- AI: Groq only ---
    ai_provider: str = "groq"
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    ai_polish: bool = _bool("AI_POLISH", True)
    # NOTE: llama-3.3-70b-versatile (the old default) was retired by Groq on
    # 16 Aug 2026 - requests to it now fail, so a valid API key was not enough
    # to make the assistant answer. openai/gpt-oss-20b is the current
    # production replacement.
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    groq_timeout_seconds: int = _int("GROQ_TIMEOUT_SECONDS", 45)
    # gpt-oss models spend part of the budget on hidden reasoning tokens, so a
    # small cap can come back with an empty visible reply.
    groq_max_tokens: int = _int("GROQ_MAX_TOKENS", 1500)
    slot_hold_minutes: int = _int("SLOT_HOLD_MINUTES", 10)

    enable_scheduler: bool = _bool("ENABLE_SCHEDULER", True)

    @property
    def ai_mode(self) -> str:
        """How the AI front desk answers.

        local   - deterministic engine only (no API key needed, always works)
        hybrid  - deterministic engine owns the flow, Groq only re-phrases the
                  final reply in natural language   <- default when a key exists
        groq    - full Groq tool-calling conversation (opt-in, needs a key)

        Set AI_MODE in .env to force one of them.
        """
        mode = (os.getenv("AI_MODE") or "").strip().lower()
        if mode in {"local", "hybrid", "groq"}:
            return mode
        return "hybrid" if self.groq_api_key else "local"

    @property
    def mysql_url(self) -> str:
        from urllib.parse import quote_plus

        return (
            f"mysql+pymysql://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def primary_url(self) -> str:
        return self.database_url_override or self.mysql_url

    def database_config_warnings(self) -> list[str]:
        """Flag a .env that configures the same database twice, inconsistently.

        DATABASE_URL and the DB_* variables can both be set. Since DB_* takes
        precedence when it is EXPLICITLY provided, a stale copy in DATABASE_URL
        would otherwise still be used by the SQLAlchemy engine while every other
        helper (tests/mysql_checks.py, app/tools/*) used DB_*. That silent
        disagreement is exactly how "the app connects but the test says access
        denied" happens.
        """
        warnings: list[str] = []

        if self.password_looks_url_encoded():
            warnings.append(
                "DB_PASSWORD contains %-escapes (it looks like the URL-encoded form). "
                "DB_PASSWORD expects the RAW password - use 'Vvh@2026Secure', not "
                "'Vvh%402026Secure'. If you see 'Access denied', this is why."
            )

        parts = self._url_parts
        if not parts:
            return warnings
        explicit = {
            "host": self._db_host_env,
            "port": self._db_port_env,
            "database": self._db_name_env,
            "user": self._db_user_env,
            "password": self._db_password_env,
        }
        for field, given in explicit.items():
            if given and parts.get(field) and given != parts[field]:
                warnings.append(
                    f"DB_{field.upper()}='{given}' but DATABASE_URL says "
                    f"{field}='{parts[field]}' - DB_{field.upper()} wins, "
                    f"so DATABASE_URL is being ignored for this field."
                )
        return warnings

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def prescriptions_dir(self) -> Path:
        return self.storage_dir / "prescriptions"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for path in (s.storage_dir, s.uploads_dir, s.prescriptions_dir):
        path.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
