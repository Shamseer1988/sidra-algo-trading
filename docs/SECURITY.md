# Security Baseline

Phase 2 uses Argon2id password hashing, short-lived signed access tokens, rotating refresh-token sessions, HttpOnly authentication cookies, role-bearing claims, account lockout, Redis-backed login throttling, RBAC, CSRF validation for state-changing browser requests, structured non-secret logs, security headers, and private Docker data services. Users can revoke their active sessions and administrators can review recent audit events in the terminal.

The CSRF token is a non-HttpOnly same-site cookie paired with the `X-CSRF-Token` request header; JavaScript must never read either authentication cookie. The Telegram webhook is exempt from CSRF because it authenticates every request with Telegram's configured secret header.

The Telegram webhook accepts only an exact configured secret header, one configured chat, and explicitly allowed Telegram sender IDs. Its approval callbacks create intents only. Emergency stop is the sole callback that changes operational state; it stops the scanner and remains auditable.

Do not expose the application directly to the public internet until those controls, TLS, a reverse proxy, and firewall policy have been configured.
