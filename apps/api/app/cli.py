import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import User, UserRole
from app.db.session import SessionLocal
from app.services.auth import hash_password
from app.services.upstox_auto_auth import UpstoxAutoAuthService


def safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        safe = text.encode("ascii", errors="replace").decode("ascii")
        print(safe)


async def create_admin(email: str) -> None:
    normalized_email = email.strip().lower()
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")
    async with SessionLocal() as session:
        if await session.scalar(select(User).where(User.email == normalized_email)):
            raise SystemExit("A user with that email already exists")
        session.add(User(email=normalized_email, password_hash=hash_password(password), role=UserRole.ADMIN))
        await session.commit()
    safe_print(f"[OK] Administrator created for {normalized_email}")


async def upstox_auto_auth(auth_code: str | None = None) -> None:
    settings = get_settings()
    service = UpstoxAutoAuthService(settings)
    try:
        result = await service.execute_automated_login(auth_code_override=auth_code)
        safe_print(f"[OK] {result.message}")
        safe_print(f"• Token: {result.token_masked}")
        safe_print(f"• Valid until: {result.expires_at}")
    except Exception as exc:
        safe_print(f"[ERROR] Upstox auto-authentication failed: {exc}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="intraday-sentinel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin_parser = subparsers.add_parser("create-admin")
    create_admin_parser.add_argument("--email", required=True)

    auto_auth_parser = subparsers.add_parser("upstox-auto-auth")
    auto_auth_parser.add_argument("--code", required=False, default=None, help="Optional manual authorization code")

    arguments = parser.parse_args()
    if arguments.command == "create-admin":
        asyncio.run(create_admin(arguments.email))
    elif arguments.command == "upstox-auto-auth":
        asyncio.run(upstox_auto_auth(arguments.code))


if __name__ == "__main__":
    main()
