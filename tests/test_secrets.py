"""Docker-secrets loading: sensitive settings read from files under a secrets
directory (mounted at /run/secrets in the container), with env vars still
winning and a missing directory being a harmless no-op."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def _write(dir_: Path, name: str, value: str) -> None:
    (dir_ / name).write_text(value)


def test_reads_secrets_from_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_SECRET", raising=False)
    _write(tmp_path, "secret_key", "sk-from-file")
    _write(tmp_path, "admin_password", "pw-from-file")
    _write(tmp_path, "oidc_client_secret", "cs-from-file")

    s = Settings(_secrets_dir=str(tmp_path))
    assert s.secret_key == "sk-from-file"
    assert s.admin_password == "pw-from-file"
    assert s.oidc_client_secret == "cs-from-file"


def test_env_var_overrides_secret_file(tmp_path, monkeypatch):
    _write(tmp_path, "oidc_client_secret", "cs-from-file")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "cs-from-env")
    s = Settings(_secrets_dir=str(tmp_path))
    assert s.oidc_client_secret == "cs-from-env"


def test_missing_secrets_dir_is_noop(monkeypatch):
    # Nonexistent dir must not raise; settings fall back to env/defaults.
    monkeypatch.setenv("SECRET_KEY", "env-key")
    s = Settings(_secrets_dir=None)
    assert s.secret_key == "env-key"


def test_client_secret_secret_file_drives_oidc_configured(tmp_path, monkeypatch):
    for var in ("OIDC_CLIENT_SECRET",):
        monkeypatch.delenv(var, raising=False)
    _write(tmp_path, "oidc_client_secret", "cs-from-file")
    s = Settings(
        _secrets_dir=str(tmp_path),
        OIDC_ENABLED="true",
        OIDC_TENANT_ID="t",
        OIDC_CLIENT_ID="c",
    )
    assert s.oidc_configured is True
