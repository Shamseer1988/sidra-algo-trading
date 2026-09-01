import argparse
import asyncio
import getpass

from sqlalchemy import select

from app.db.models import User, UserRole
from app.db.session import SessionLocal
from app.services.auth import hash_password


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="intraday-sentinel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin_parser = subparsers.add_parser("create-admin")
    create_admin_parser.add_argument("--email", required=True)

    arguments = parser.parse_args()
    if arguments.command == "create-admin":
        asyncio.run(create_admin(arguments.email))


if __name__ == "__main__":
    main()
