# Windows Deployment

Use Docker Desktop with the WSL2 backend. Copy `.env.example` to `.env`, generate long random secrets, and keep the Docker published ports bound to `127.0.0.1` unless a reverse proxy and explicit LAN policy are configured.

Use `scripts/start.ps1` and `scripts/stop.ps1` for lifecycle commands. `scripts/backup-postgres.ps1` creates timestamped compressed SQL backups under `backups/`; `restore-postgres.ps1` requires an explicit backup path.
