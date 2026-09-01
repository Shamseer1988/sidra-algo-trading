# Upstox Authorization Policy

Browser automation for mobile number, PIN, or TOTP entry is intentionally unsupported and has been removed. Sidra uses the broker-supported OAuth authorization flow only.

An administrator renews authorization from **Settings → Market-data connectors → Renew Upstox access**. Credentials and tokens remain server-side, the callback validates OAuth state, and the stored access token is encrypted.

If authorization expires, the scanner reports the connector as unavailable and remains safely stopped. Sidra never stores or automates a broker PIN or TOTP secret.
