# Docker secrets

knowts reads its sensitive values from files here (mounted read-only at
`/run/secrets/<name>` in the container) instead of environment variables, so
they never show up in `docker inspect` or `/proc/<pid>/environ`.

| File                  | Setting              | Notes                                       |
| --------------------- | -------------------- | ------------------------------------------- |
| `secret_key`          | `SECRET_KEY`         | Signs session cookies. High-entropy, stable.|
| `admin_password`      | `ADMIN_PASSWORD`     | Bootstrap / break-glass admin password.     |
| `oidc_client_secret`  | `OIDC_CLIENT_SECRET` | Entra app client secret. Empty if SSO off.  |

Create them once with:

```sh
./deploy/init-secrets.sh
```

The actual secret files are **gitignored** — never commit them. Only this
README is tracked. `chmod 600` is applied by the init script (`umask 077`).

Non-secret configuration (tenant/client IDs, redirect URL, admin emails, etc.)
stays in `.env`.
