"""Application configuration, loaded from environment variables.

See PLAN.md §9 for the full list. Only the variables needed by Phase 1
(skeleton + auth) are consumed today; the STT/LLM settings are declared here so
the contract is stable for later phases and documented in one place.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage -----------------------------------------------------------
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")

    # --- Sessions / security ----------------------------------------------
    # A random key is generated if unset so the app still boots, but sessions
    # will not survive a restart and every replica would sign differently.
    # Set SECRET_KEY explicitly in any real deployment.
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48), alias="SECRET_KEY")
    session_cookie_name: str = Field(default="knowts_session", alias="SESSION_COOKIE_NAME")
    # Idle timeout: a session expires this many seconds after the last request.
    session_idle_seconds: int = Field(default=60 * 60 * 12, alias="SESSION_IDLE_SECONDS")
    # Absolute lifetime: a session is force-expired this long after creation,
    # regardless of activity.
    session_absolute_seconds: int = Field(default=60 * 60 * 24 * 7, alias="SESSION_ABSOLUTE_SECONDS")
    # Set to true when served over HTTPS so the cookie carries the Secure flag.
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")

    # --- Admin bootstrap ---------------------------------------------------
    admin_user: str | None = Field(default=None, alias="ADMIN_USER")
    admin_password: str | None = Field(default=None, alias="ADMIN_PASSWORD")

    # --- Login rate limiting ----------------------------------------------
    login_max_attempts: int = Field(default=5, alias="LOGIN_MAX_ATTEMPTS")
    login_lockout_seconds: int = Field(default=15 * 60, alias="LOGIN_LOCKOUT_SECONDS")

    # --- Uploads (used from Phase 2) --------------------------------------
    max_upload_mb: int = Field(default=500, alias="MAX_UPLOAD_MB")

    # --- STT / LLM upstreams (used from Phase 2) --------------------------
    llm_base_url: str = Field(default="http://link:8080/v1", alias="LLM_BASE_URL")
    llm_default_model: str = Field(default="qwen2.5-32b", alias="LLM_DEFAULT_MODEL")
    llm_context_tokens: int = Field(default=32768, alias="LLM_CONTEXT_TOKENS")
    stt_base_url: str | None = Field(default=None, alias="STT_BASE_URL")
    stt_model: str = Field(default="whisper-large-v3-turbo", alias="STT_MODEL")
    # Language hint sent to whisper (ISO-639-1, e.g. "nl"). Controls the
    # transcription *source* language so whisper doesn't autodetect-and-guess
    # (a Dutch meeting whose intro sounds English can otherwise come back
    # transcribed as English). Three modes:
    #   unset/empty  -> derive per meeting from the owner's UI language
    #                   (non-English preference pins that language; English
    #                   falls back to autodetect)
    #   "auto"       -> always let whisper autodetect, ignore the preference
    #   "<code>"     -> always pin this language for every transcription
    stt_language: str | None = Field(default=None, alias="STT_LANGUAGE")

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "knowts.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def effective_stt_base_url(self) -> str:
        """STT defaults to the LLM base URL (single llama-swap endpoint)."""
        return self.stt_base_url or self.llm_base_url

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
