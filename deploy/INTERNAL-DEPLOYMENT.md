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

## 3. Choose how TLS is terminated

**TLS is optional and decoupled from SSO.** knowts always serves plain HTTP on
port 8000; something in front should add HTTPS for a real publish. Pick the mode
that fits your environment — set these in `.env`:

| Mode | `KNOWTS_BIND` | `tls` profile | `COOKIE_SECURE` | You browse |
| --- | --- | --- | --- | --- |
| **A. Bundled Caddy** terminates TLS | `127.0.0.1` | yes | `true` | `https://$KNOWTS_DOMAIN` (443) |
| **B. Your own reverse proxy** terminates TLS | `0.0.0.0` | no | `true` | your proxy → `http://<host>:8000` |
| **C. Plain HTTP** (dev / trusted LAN) | `0.0.0.0` | no | `false` | `http://<host>:8000` |

> `COOKIE_SECURE=true` requires the browser to actually be on HTTPS (modes A/B).
> Setting it `true` in mode C would stop the session cookie being sent.

### Mode A — bundled Caddy

The compose file ships a Caddy reverse proxy behind the `tls` profile; it listens
on 443 and forwards to knowts (kept on loopback, so nothing serves plain HTTP on
the network). Pick a cert strategy in [`deploy/Caddyfile`](./Caddyfile):

| Cert | When to use | Browser trust |
| --- | --- | --- |
| `tls internal` *(default)* | Quickest; no cert to manage | Warning unless you distribute Caddy's root CA |
| `tls /certs/knowts.crt /certs/knowts.key` | You have a cert from a corporate/internal CA machines already trust | Trusted, no warning |
| `tls { dns <provider> {env.TOKEN} }` | You own a public DNS zone + API token | Publicly trusted |

- **Internal CA (default):** export Caddy's root CA after first start and push it
  to office machines via MDM/GPO to remove the warning:
  ```bash
  docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./knowts-root-ca.crt
  ```
- **Own cert:** drop `knowts.crt` + `knowts.key` into `deploy/certs/` and switch
  the `tls` line in the Caddyfile (files are gitignored).

Point internal DNS `KNOWTS_DOMAIN` at the Docker host.

### Mode B — your own reverse proxy

Expose knowts on the LAN and terminate TLS at your existing proxy / load
balancer (nginx, Traefik, HAProxy, a corporate LB, …). Do **not** start the
`tls` profile. Your proxy must:

- forward to `http://<docker-host>:8000`;
- send `X-Forwarded-Proto: https` (so the app sets Secure cookies and correct
  redirect URLs — knowts already runs uvicorn with `--proxy-headers`);
- for large uploads, allow a big request body (see `MAX_UPLOAD_MB`).

Optionally set `FORWARDED_ALLOW_IPS` to your proxy's source IP so LAN clients
can't spoof the scheme. Example nginx server block:

```nginx
server {
    listen 443 ssl;
    server_name knowts.corp.example;
    ssl_certificate     /etc/ssl/knowts.crt;
    ssl_certificate_key /etc/ssl/knowts.key;
    client_max_body_size 1g;

    location / {
        proxy_pass http://<docker-host>:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For   $remote_addr;
    }
}
```

Set `OIDC_REDIRECT_URL=https://knowts.corp.example/auth/sso/callback` to match
the public hostname your proxy serves.

---

## 4. Launch

**Mode A (bundled Caddy):**
```bash
docker compose --profile tls up -d --build   # knowts + caddy; browse https://$KNOWTS_DOMAIN
```

**Mode B / C (own proxy or plain HTTP):**
```bash
docker compose up -d --build                 # knowts only, on <host>:8000
```

Then:
- Click **Sign in with Microsoft** → Entra login → back to knowts, signed in.
- Verify an admin email lands as admin (Users page visible) and a normal
  colleague lands as a member.

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
