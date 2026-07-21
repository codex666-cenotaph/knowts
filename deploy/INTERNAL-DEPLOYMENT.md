# Publishing knowts internally: Entra ID SSO + TLS

This guide covers publishing knowts to your organisation on the office network
(and over VPN) — **not** to the public internet — with:

1. **Microsoft Entra ID (Azure AD) single sign-on**, so colleagues sign in with
   their work account, and
2. **TLS/HTTPS** in front of the app via a Caddy reverse proxy.

Everything here is driven by environment variables (`.env`) and
`docker-compose.yml`; no code changes are needed.

---

## 0. How auth works after this change

- The login page keeps the local username/password form **and** adds a
  **"Sign in with Microsoft"** button (hybrid mode — the default).
- On first SSO login, knowts **provisions the account just-in-time**: the person
  becomes a `member`, keyed to their Entra object id + email. Emails listed in
  `OIDC_ADMIN_EMAILS` become `admin`. You can promote/demote anyone later on the
  **Users** admin page.
- SSO accounts have no local password (they can only sign in via Microsoft).
- The bootstrap admin (`ADMIN_USER`/`ADMIN_PASSWORD`) remains a **break-glass**
  local login even if you later hide the local form (`LOCAL_LOGIN_ENABLED=false`
  → the form is hidden but still reachable at `/login?local=1`).

---

## 1. Register the app in Entra ID

In the [Entra admin center](https://entra.microsoft.com) → **Applications → App
registrations → New registration**:

1. **Name**: `knowts` (anything).
2. **Supported account types**: *Accounts in this organizational directory only*
   (single tenant).
3. **Redirect URI**: platform **Web**, value:
   `https://<KNOWTS_DOMAIN>/auth/sso/callback`
   (e.g. `https://knowts.corp.example/auth/sso/callback`). This must match
   `OIDC_REDIRECT_URL` **exactly**.
4. **Register**.

From the app's **Overview**, copy:
- **Application (client) ID** → `OIDC_CLIENT_ID`
- **Directory (tenant) ID** → `OIDC_TENANT_ID`

Then **Certificates & secrets → New client secret**, copy the secret **Value**
(not the Secret ID) → `OIDC_CLIENT_SECRET`. Note its expiry and set a reminder to
rotate it.

**API permissions** are fine as-is: the default `User.Read` (delegated) plus the
OIDC scopes `openid`, `email`, `profile` are all knowts requests. No admin
consent is normally required for these.

> Restricting *who* can use the app: either set
> `OIDC_ALLOWED_EMAIL_DOMAIN=example.com` in knowts, or (stronger) in Entra set
> the app to **Assignment required** under *Enterprise applications → knowts →
> Properties*, then assign the users/groups who may sign in.

---

## 2. Configure knowts

Configuration splits in two: **secrets** live in files under `./secrets/`
(mounted into the container at `/run/secrets`, kept out of `docker inspect` and
the process environment); everything **non-secret** lives in `.env`.

### 2a. Create the secret files

```sh
./deploy/init-secrets.sh
```

This writes three gitignored files (see [`secrets/README.md`](../secrets/README.md)):

| File                         | Setting              | Action                                    |
| ---------------------------- | -------------------- | ----------------------------------------- |
| `secrets/secret_key`         | `SECRET_KEY`         | Auto-generated (stable, high-entropy).    |
| `secrets/admin_password`     | `ADMIN_PASSWORD`     | Edit — set the break-glass admin password.|
| `secrets/oidc_client_secret` | `OIDC_CLIENT_SECRET` | Paste the Entra client secret **Value**.  |

```sh
printf '%s' 'a-strong-break-glass-password' > secrets/admin_password
printf '%s' '<entra client secret value>'   > secrets/oidc_client_secret
./deploy/init-secrets.sh   # re-run to repair file permissions after editing
```

> The container runs as a non-root user, so the secret files must stay readable
> (`0644`); the `./secrets` directory is `0700` so other host users can't read
> them. `init-secrets.sh` sets this — re-run it (or `chmod 644 secrets/*`) if you
> edit a file and your shell's umask tightens it, otherwise the container fails
> to start with `PermissionError: /run/secrets/...`.

### 2b. Set non-secret config in `.env`

```dotenv
ADMIN_USER=marco

# Served over HTTPS via the proxy:
COOKIE_SECURE=true
KNOWTS_DOMAIN=knowts.corp.example

# Entra ID SSO (client secret is a secret file, not here)
OIDC_ENABLED=true
OIDC_TENANT_ID=<directory (tenant) id>
OIDC_CLIENT_ID=<application (client) id>
OIDC_REDIRECT_URL=https://knowts.corp.example/auth/sso/callback
OIDC_ADMIN_EMAILS=marco@example.com
OIDC_ALLOWED_EMAIL_DOMAIN=example.com   # optional
LOCAL_LOGIN_ENABLED=true                # keep break-glass local login
```

> Do **not** also put `SECRET_KEY`, `ADMIN_PASSWORD`, or `OIDC_CLIENT_SECRET` in
> `.env` for the Docker deployment — an env var would override the secret file.
> (They belong in `.env` only when running the app directly with `uvicorn`.)

---

## 3. Terminate TLS with Caddy

The compose file ships a Caddy reverse proxy behind the `tls` profile. It listens
on 443 and forwards to knowts (which itself only binds to loopback now, so
nothing serves plain HTTP on the network).

Pick a TLS mode in [`deploy/Caddyfile`](./Caddyfile):

| Mode | When to use | Browser trust |
| --- | --- | --- |
| `tls internal` *(default)* | Quickest; no cert to manage | Warning unless you distribute Caddy's root CA |
| `tls /certs/knowts.crt /certs/knowts.key` | You have a cert from a corporate/internal CA machines already trust | Trusted, no warning |
| `tls { dns <provider> {env.TOKEN} }` | You own a public DNS zone + API token | Publicly trusted |

- **Internal CA (default):** after first start, export Caddy's root CA and push
  it to office machines via MDM/GPO to remove the warning:
  ```bash
  docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./knowts-root-ca.crt
  ```
- **Own cert:** drop `knowts.crt` + `knowts.key` into `deploy/certs/` and switch
  the `tls` line in the Caddyfile. (These files are gitignored.)

Point your internal DNS `KNOWTS_DOMAIN` (e.g. `knowts.corp.example`) at the host
running Docker.

---

## 4. Launch

```bash
docker compose --profile tls up -d --build
```

- `knowts` and `caddy` both start; browse to `https://<KNOWTS_DOMAIN>`.
- Click **Sign in with Microsoft** → Entra login → back to knowts, signed in.
- Verify an admin email lands as admin (Users page visible) and a normal
  colleague lands as a member.

Without the profile (`docker compose up -d`) only knowts runs, on
`http://127.0.0.1:8000` — handy for local dev, but not for the internal publish.

---

## 5. Operating notes

- **Rotating the client secret:** create a new secret in Entra, overwrite
  `secrets/oidc_client_secret`, then `docker compose up -d` to restart knowts.
  Same pattern for `secrets/secret_key` (note: rotating it invalidates all
  active sessions) and `secrets/admin_password`.
- **Adding admins:** either add the email to `OIDC_ADMIN_EMAILS` and restart, or
  promote the existing member on the **Users** page (no restart).
- **Removing access:** unassign the user in Entra (if assignment is required)
  and/or deactivate them on the Users page. Deactivated users are refused at SSO
  callback too.
- **Redirect URI mismatch (`AADSTS50011`):** `OIDC_REDIRECT_URL`, the Entra
  redirect URI, and the URL colleagues actually use must all be the same
  `https://<host>/auth/sso/callback`.
- **`invalid_client`:** wrong/expired `OIDC_CLIENT_SECRET` (make sure you copied
  the secret *Value*, not its ID).
