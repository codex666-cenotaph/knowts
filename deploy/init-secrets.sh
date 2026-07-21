#!/usr/bin/env sh
# Create the local Docker-secret files knowts reads at runtime (mounted at
# /run/secrets in the container). Run once before `docker compose up`.
#
#   ./deploy/init-secrets.sh
#
# The files live in ./secrets/ and are gitignored — never commit them. This
# script never overwrites a file that already has content, so it is safe to
# re-run (re-running also repairs the file permissions below). Edit the values
# afterwards as needed.
#
# Permissions: the knowts container runs as a non-root user (uid 10001), and a
# bind-mounted secret keeps its host permissions — so the files must be
# world-readable (0644) or the container cannot read them. Plain `docker
# compose` (non-Swarm) ignores per-secret uid/gid/mode, so this is the reliable
# way. Host-side confidentiality comes from the directory instead: ./secrets is
# 0700, so other host users cannot enter it (the Docker daemon mounts as root
# and is unaffected).
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
mkdir -p secrets
chmod 700 secrets

# SECRET_KEY — signs session cookies. Must be high-entropy and stable across
# restarts. Generated only if missing/empty.
if [ ! -s secrets/secret_key ]; then
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n' > secrets/secret_key
  else
    python3 -c "import secrets;print(secrets.token_urlsafe(48),end='')" > secrets/secret_key
  fi
  echo "Generated secrets/secret_key"
else
  echo "Kept existing secrets/secret_key"
fi

# ADMIN_PASSWORD — bootstrap/break-glass admin password. Placeholder; edit it.
if [ ! -e secrets/admin_password ]; then
  printf '%s' 'change-me' > secrets/admin_password
  echo "Wrote secrets/admin_password (placeholder 'change-me' — CHANGE THIS)"
fi

# OIDC_CLIENT_SECRET — Entra app client secret. Empty is fine when SSO is off;
# paste the secret Value here when enabling SSO.
if [ ! -e secrets/oidc_client_secret ]; then
  : > secrets/oidc_client_secret
  echo "Created empty secrets/oidc_client_secret (fill in when enabling SSO)"
fi

# Readable by the non-root container user (see header note). Re-applied every
# run so files created by an older version of this script are repaired too.
chmod 0644 secrets/secret_key secrets/admin_password secrets/oidc_client_secret

echo
echo "Done. Next: set ADMIN_USER + the OIDC_* (non-secret) values in .env,"
echo "then run:  docker compose --profile tls up -d --build"
