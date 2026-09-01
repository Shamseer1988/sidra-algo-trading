# Scheduled Daily Authentication (Upstox 2FA TOTP)

This guide documents the automated cron / scheduler setup that automatically renews your Upstox access token every morning before market hours.

---

## 1. Windows Task Scheduler (Active)

On Windows, the job is configured via Windows Task Scheduler.

### Configuration Details:
- **Task Name**: `IntradaySentinel-UpstoxDailyAuth`
- **Schedule**: Every Monday to Friday at **08:30 AM IST**
- **Action**: `powershell.exe -ExecutionPolicy Bypass -File E:\Intraday_algo\scripts\upstox-daily-auth.ps1 -Docker`
- **Behavior**: Wakes system if asleep, generates TOTP via `pyotp`, logs in headlessly via Playwright Chromium in Docker, persists the encrypted token, and sends a **Telegram notification**.

### Management Commands:

```powershell
# Check task status:
Get-ScheduledTask -TaskName 'IntradaySentinel-UpstoxDailyAuth'

# Manually test trigger:
Start-ScheduledTask -TaskName 'IntradaySentinel-UpstoxDailyAuth'

# Change execution time (e.g., 08:00 AM):
.\scripts\setup-scheduled-auth.ps1 -Time "08:00AM"

# Remove scheduled task:
.\scripts\setup-scheduled-auth.ps1 -Uninstall
```

---

## 2. Linux / Server Crontab (Optional)

If deploying to a Linux server or VPS:

1. Make script executable:
   ```bash
   chmod +x ./scripts/upstox-daily-auth.sh
   ```

2. Open crontab:
   ```bash
   crontab -e
   ```

3. Add entry for **08:30 AM IST (03:00 AM UTC)** Monday to Friday:
   ```cron
   # Upstox Daily Auto-Auth at 08:30 AM IST (03:00 AM UTC)
   0 3 * * 1-5 cd /opt/intraday-sentinel && ./scripts/upstox-daily-auth.sh >> /var/log/upstox-auth.log 2>&1
   ```
