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

    # --- Local (username/password) login ----------------------------------
    # When false, the login page hides the local password form and funnels
    # colleagues to "Sign in with Microsoft". The form still works if reached
    # directly (``/login?local=1``) so a break-glass admin can always get in;
    # it is also always shown when SSO is not configured, so the app can never
    # lock everyone out.
    local_login_enabled: bool = Field(default=True, alias="LOCAL_LOGIN_ENABLED")

    # --- Microsoft Entra ID (Azure AD) SSO via OpenID Connect -------------
    # Off by default. When enabled and the tenant/client credentials below are
    # set, the login page offers a "Sign in with Microsoft" button and runs the
    # OIDC authorization-code flow against the tenant. See
    # deploy/INTERNAL-DEPLOYMENT.md for the Entra app-registration steps.
    oidc_enabled: bool = Field(default=False, alias="OIDC_ENABLED")
    # Directory (tenant) ID — single-tenant. Use "organizations" only if you
    # deliberately want any work/school account; a specific GUID is recommended.
    oidc_tenant_id: str | None = Field(default=None, alias="OIDC_TENANT_ID")
    oidc_client_id: str | None = Field(default=None, alias="OIDC_CLIENT_ID")
    oidc_client_secret: str | None = Field(default=None, alias="OIDC_CLIENT_SECRET")
    # Full public callback URL registered on the Entra app, e.g.
    # https://knowts.corp.example/auth/sso/callback. Left blank, it is derived
    # from the incoming request (needs the reverse proxy to forward the scheme).
    oidc_redirect_url: str | None = Field(default=None, alias="OIDC_REDIRECT_URL")
    # Comma/space-separated emails that should be granted admin on SSO login.
    # Everyone else is provisioned as a member; promote others via /admin/users.
    oidc_admin_emails: str = Field(default="", alias="OIDC_ADMIN_EMAILS")
    # Optional hard restriction: only accept accounts whose email is in this
    # domain (e.g. "example.com"). Blank accepts any email the tenant returns.
    oidc_allowed_email_domain: str | None = Field(
        default=None, alias="OIDC_ALLOWED_EMAIL_DOMAIN"
    )
    oidc_button_label: str = Field(
        default="Sign in with Microsoft", alias="OIDC_BUTTON_LABEL"
    )

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

    # --- Speaker diarization (optional, off by default) -------------------
    # In-container CPU diarization via sherpa-onnx (no GPU, no HF token). When
    # enabled, the pipeline labels each transcript segment with a speaker.
    # Requires the extra deps (requirements-diarization.txt) and the two
    # non-gated ONNX models (paths below). See README for setup.
    diarization_enabled: bool = Field(default=False, alias="DIARIZATION_ENABLED")
    diarization_segmentation_model: str | None = Field(
        default=None, alias="DIARIZATION_SEGMENTATION_MODEL"
    )
    diarization_embedding_model: str | None = Field(
        default=None, alias="DIARIZATION_EMBEDDING_MODEL"
    )
    # Number of speakers if known ahead of time; <= 0 clusters automatically
    # using the threshold below.
    diarization_num_speakers: int = Field(default=0, alias="DIARIZATION_NUM_SPEAKERS")
    diarization_cluster_threshold: float = Field(
        default=0.5, alias="DIARIZATION_CLUSTER_THRESHOLD"
    )
    diarization_num_threads: int = Field(default=1, alias="DIARIZATION_NUM_THREADS")

    @property
    def oidc_configured(self) -> bool:
        """True only when SSO is enabled *and* the tenant/client credentials
        are all present. Route guards and the login page key off this."""
        return bool(
            self.oidc_enabled
            and self.oidc_tenant_id
            and self.oidc_client_id
            and self.oidc_client_secret
        )

    @property
    def oidc_admin_email_set(self) -> frozenset[str]:
        """Lowercased set of admin emails parsed from ``OIDC_ADMIN_EMAILS``
        (comma- or whitespace-separated)."""
        raw = self.oidc_admin_emails.replace(",", " ").split()
        return frozenset(e.strip().lower() for e in raw if e.strip())

    @property
    def diarization_configured(self) -> bool:
        """True only when diarization is enabled *and* both model paths are set
        (existence is checked at load time in app/diarize.py)."""
        return bool(
            self.diarization_enabled
            and self.diarization_segmentation_model
            and self.diarization_embedding_model
        )

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
