# Security Baseline

Phase 1 uses Argon2id password hashing, short-lived signed access tokens, HttpOnly authentication cookies, role-bearing claims, structured non-secret logs, and private Docker data services. CSRF protection, refresh-token rotation, login rate limits, account lockout, and security headers are Phase 2 completion items before public exposure.

Do not expose the application directly to the public internet until those controls, TLS, a reverse proxy, and firewall policy have been configured.
