"""Microsoft Entra ID (Azure AD) SSO via OpenID Connect.

Optional, off by default. When ``OIDC_ENABLED=true`` and the tenant/client
credentials are configured (see :class:`app.config.Settings`), the login page
offers "Sign in with Microsoft" and the authorization-code flow runs against
the tenant. Authlib handles state, nonce, and validating the ``id_token``
against the tenant's published JWKS; this module only turns the resulting
claims into a knowts user, provisioning members just-in-time.

Authlib is imported lazily inside :func:`build_oauth` so it is only a hard
dependency when SSO is actually enabled.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import users as users_mod
from .config import Settings
from .users import User

# Single-tenant discovery document. ``{tenant}`` is the directory (tenant) ID.
DISCOVERY_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
)
CLIENT_NAME = "microsoft"


def build_oauth(settings: Settings):
    """Construct an Authlib OAuth registry with the Entra client registered.

    The server metadata (and JWKS) are fetched lazily by Authlib on first use,
    so calling this at startup performs no network I/O and never blocks boot.
    """
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name=CLIENT_NAME,
        server_metadata_url=DISCOVERY_TEMPLATE.format(tenant=settings.oidc_tenant_id),
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


@dataclass(frozen=True)
class OidcIdentity:
    """The subset of Entra ``id_token`` claims knowts cares about."""

    subject: str  # stable per-tenant object id (`oid`), falls back to `sub`
    email: str
    display_name: str

    @property
    def email_domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower() if "@" in self.email else ""


def identity_from_claims(claims: dict) -> OidcIdentity | None:
    """Extract an :class:`OidcIdentity` from validated id_token/userinfo claims.

    Returns ``None`` when the token lacks a stable subject or any email — knowts
    keys accounts on email, so an identity without one cannot be provisioned.
    """
    subject = claims.get("oid") or claims.get("sub")
    email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    ).strip().lower()
    name = (claims.get("name") or email or subject or "").strip()
    if not subject or not email or "@" not in email:
        return None
    return OidcIdentity(subject=str(subject), email=email, display_name=name)


def is_email_allowed(settings: Settings, identity: OidcIdentity) -> bool:
    """Honour the optional single-domain restriction."""
    domain = (settings.oidc_allowed_email_domain or "").strip().lower()
    if not domain:
        return True
    return identity.email_domain == domain


def should_be_admin(settings: Settings, identity: OidcIdentity) -> bool:
    return identity.email in settings.oidc_admin_email_set


def resolve_user(
    conn: sqlite3.Connection, settings: Settings, identity: OidcIdentity
) -> User:
    """Find-or-create the knowts user for this Entra identity.

    Resolution order: match the stable ``oid`` first; else adopt a pre-existing
    account by email (or by a username that equals the email — e.g. an admin
    seeded before SSO) and link the ``oid`` to it; else provision a new member.
    Admin membership is synced from the allowlist on every login (promote-only).
    """
    is_admin = should_be_admin(settings, identity)

    user = users_mod.get_by_oidc_subject(conn, identity.subject)
    if user is None:
        existing = users_mod.get_by_email(conn, identity.email) or users_mod.get_by_username(
            conn, identity.email
        )
        if existing is not None:
            users_mod.link_oidc_subject(
                conn, existing.id, identity.subject, email=identity.email
            )
            user = users_mod.get_by_id(conn, existing.id)

    if user is None:
        return users_mod.provision_sso(
            conn,
            subject=identity.subject,
            email=identity.email,
            role="admin" if is_admin else "member",
        )

    users_mod.sync_sso_profile(
        conn, user.id, email=identity.email, promote_admin=is_admin
    )
    refreshed = users_mod.get_by_id(conn, user.id)
    assert refreshed is not None
    return refreshed
