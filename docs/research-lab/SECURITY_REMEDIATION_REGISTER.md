# Security remediation register

Phase 1 review, 27 November 2026. Findings only; nothing here was changed except where the Phase 1
implementation report says so. Severity is for this deployment as it is configured today.

Exposure model, verified:

* **Internet:** one Cloudflare tunnel (`cloudflared-tunnel`, host network, token mode; its routes live in
  the Cloudflare dashboard, not on the NAS). From outside, `https://telegram.faizzyworld.com` answers
  **only** `/api/v1/telegram/webhook` (a GET returns 405 from FastAPI). `/`, `/login`,
  `/api/v1/health/live`, `/api/v1/openapi.json` and `/api/v1/telegram/status` return 404.
* **LAN:** Caddy with an internal certificate on `0.0.0.0:8443` proxies the whole web app, including the
  `/api/*` rewrite to the API. The web container itself is bound to `127.0.0.1:3001`.
* **Host loopback:** Postgres `127.0.0.1:5433`, Redis `127.0.0.1:6379`.

| # | Severity | Finding | Evidence | Recommended remediation | Operational impact |
|---|---|---|---|---|---|
| S1 | High | Upstox PIN, TOTP seed and mobile number are plain environment variables, so anyone who can read `.env` or `docker inspect` the containers can log in to the broker account | `.env` names; `upstox_auto_auth.py` reads `settings.upstox_pin`, `upstox_totp_secret` | Move to Docker secrets mounted as files readable only by the container user, or to the existing encrypted `credential_store` with the key held outside `.env`. Restrict `.env` to mode 600, owner root. Longer term, ask Upstox whether a supported non-interactive token flow exists | Needs a change to how settings load and a redeploy; morning auto-renewal must be retested on a non-trading day |
| S2 | High | Firstock password and TOTP seed, same pattern | `.env` names | Same as S1. Firstock is not the selected broker; if it will not be used, remove the values from `.env` entirely | Low: not in use |
| S3 | Medium | No second factor on web login, while an ADMIN session can switch a strategy to LIVE, change risk limits, arm trading and promote sweep results | `routes/auth.py`; roles in `deps.py` | TOTP for ADMIN accounts (pyotp is already a dependency), with recovery codes. Until then: keep the UI off the internet (it is), use a long unique password, keep the 15-minute access token | New migration (user TOTP secret); UI change; ask first |
| S4 | Medium | Auto-login error messages include raw broker responses and URLs, and those messages go to the audit log, Redis status and Telegram | `upstox_auto_auth.py` raises with `{otp_data}`, `{pin_data}`, `{auth_data}`, `{res_dialog.url}` | Report the step and HTTP status only; keep a redacted body behind a debug flag. Do not change the login flow itself | Small code change in the broker-auth module; ask first (the brief forbids changing the broker authentication flow in this phase) |
| S5 | Low | `/api/v1/openapi.json` is served without authentication and is reachable on the LAN through the web proxy. Not reachable from the internet | `main.py` `openapi_url`; LAN path via Caddy → web → `/api/*` | Serve the schema only when `APP_ENV != production`, or behind `CurrentUser`. Remove `/docs` in production | None for operators; developers lose the schema in production |
| S6 | Low | The public webhook route depends on a Cloudflare dashboard rule that is not recorded anywhere in the repository | probe results above; tunnel runs with a token, no local config | Write the tunnel's ingress rules into `docs/` (hostname → `http://127.0.0.1:<port>`, path restricted to `/api/v1/telegram/webhook`). Consider a Cloudflare WAF rule allowing only Telegram's published IP ranges | Documentation, and an optional Cloudflare change |
| S7 | Low | Webhook authenticity relies on the shared secret header | `routes/telegram.py` compares `X-Telegram-Bot-Api-Secret-Token` in constant time, and an allowed-user list | Adequate. Add the IP restriction from S6 if wanted | None |
| S8 | Low | Logging has no redaction processor; protection depends on each call site not logging secrets | `core/logging.py`; order clients redact their own errors (`_redact` in `upstox_orders.py`, `firstock/orders.py`) | Add a structlog processor that masks keys named `*token*`, `*secret*`, `*password*`, `*pin*`, `authorization` | Small; no behaviour change |
| S9 | Info | Another project's Postgres (`ha-postgres`) is published on `0.0.0.0:5433` on the same host | `docker ps` | Bind it to loopback if LAN access is not needed. Outside this repository | None for SIDRA |
| S10 | Info | `scratch_upstox_test.py` in the repository root | 46 bytes; not opened | Review and delete if unused | None |

Verified as acceptable:

* Secrets are not returned by any diagnostic route read in Phase 0 and 1; `audit_deployment.py` and
  `check_settings.py` print credentials as set/unset only.
* The new `verify_deployment.py` reads one variable, `SIDRA_BUILD_COMMIT`, and prints it only if it is a
  hex hash; it never prints a container's stderr. Tests assert this.
* `COOKIE_SECURE=true` and `APP_ENV=production` on the NAS.

Suggested order: S1 and S2 together (one settings-loading change), then S3, then S4, then S5 and S8,
with S6 documentation at any time.
