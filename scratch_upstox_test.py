import asyncio
import httpx
from app.core.config import get_settings

async def test_endpoints():
    settings = get_settings()
    mobile = settings.upstox_mobile_number or "9876543210"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://login.upstox.com",
        "Referer": "https://login.upstox.com/",
        "Content-Type": "application/json",
    }
    endpoints = [
        "https://service.upstox.com/login/open/v5/auth/1fa/otp/generate",
        "https://api.upstox.com/v2/login/open/v5/auth/1fa/otp/generate",
        "https://api-v2.upstox.com/login/open/v5/auth/1fa/otp/generate"
    ]
    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        for ep in endpoints:
            try:
                res = await client.post(ep, json={"data": {"mobileNumber": mobile}})
                print(f"{ep} -> {res.status_code} : {res.text[:200]}")
            except Exception as e:
                print(f"{ep} -> Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_endpoints())
