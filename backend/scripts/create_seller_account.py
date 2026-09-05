"""
Create (or reset) a Seller Portal login on a freshly-deployed instance.

Every backend startup already auto-seeds ONE default seller admin (see
main.py's _seed_seller_admin(), driven by Settings.first_seller_admin_email/
first_seller_admin_password — env vars FIRST_SELLER_ADMIN_EMAIL /
FIRST_SELLER_ADMIN_PASSWORD, defaulting to seller@platform-admin.com /
Seller@1234! if unset). That covers "there is always *a* seller login" out
of the box with zero extra steps.

This script is for the two things the auto-seed can't do: create a seller
account with YOUR OWN chosen email/password/name on a new deployment (so
you're not stuck with the shared default credentials), or reset the
password on an existing seller account you're locked out of. Run it once
against the target system after the backend has started at least once
(so the `users` table exists) — or it will create the table itself via
init_db() if run before the app has ever started.

Usage (run from backend/):
    python scripts/create_seller_account.py --email you@yourcompany.com --password 'Str0ng!Pass' --name "Jane Doe"

    # Use every default (mirrors the auto-seeded account, but forces a
    # password reset if it already exists with a different one):
    python scripts/create_seller_account.py --reset-password

    # Non-admin seller sub-role:
    python scripts/create_seller_account.py --email support@yourcompany.com --password '...' \\
        --name "Support Rep" --role SELLER_SUPPORT

Valid --role values: SELLER_ADMIN (default, full Seller-tier access),
SELLER_CUSTOMER_MANAGER, SELLER_LICENSE_MANAGER, SELLER_SUPPORT,
SELLER_VIEWER. See core/deps.SELLER_ROLE_ACTIONS for what each can do.
"""
import argparse
import asyncio
import getpass
import os
import sys

# Running this file directly (`python scripts/create_seller_account.py`)
# puts scripts/ itself on sys.path[0], not backend/ — so `import app...`
# below would fail with ModuleNotFoundError even when run from the right
# directory. Insert backend/ (this file's parent's parent) explicitly so
# it works both as `python scripts/create_seller_account.py` from backend/
# and as `python create_seller_account.py` from inside scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing app.main (not just app.database.connection) pulls in every
# model module it imports at top-level, which is what registers every
# table — including `users` — on PlatformBase.metadata/TenantBase.metadata
# before init_db()'s create_all() runs. Importing it does NOT start the
# app or run its lifespan (uvicorn does that separately) — it only builds
# the FastAPI() object and its routers, exactly like uvicorn's own import
# does, so this is safe to do from a standalone script.
import app.main  # noqa: F401  (import-for-side-effects — registers all models)

from app.core.config import get_settings
from app.core.security import hash_password
from app.database.connection import AsyncSessionLocal, init_db
from app.models.user import User, UserRole
from sqlalchemy import select

SELLER_ROLES = {
    "SELLER_ADMIN",
    "SELLER_CUSTOMER_MANAGER",
    "SELLER_LICENSE_MANAGER",
    "SELLER_SUPPORT",
    "SELLER_VIEWER",
}


def parse_args():
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Create or reset a Seller Portal account on this deployment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--email", default=settings.first_seller_admin_email,
        help=f"Login email (default: {settings.first_seller_admin_email}).",
    )
    parser.add_argument(
        "--password", default=None,
        help="Login password. If omitted, you'll be prompted (hidden input) "
             "so it never lands in shell history.",
    )
    parser.add_argument(
        "--name", default="Seller Admin",
        help="Full name shown in the UI (default: 'Seller Admin').",
    )
    parser.add_argument(
        "--role", default="SELLER_ADMIN", choices=sorted(SELLER_ROLES),
        help="Seller sub-role (default: SELLER_ADMIN — full Seller-tier access).",
    )
    parser.add_argument(
        "--reset-password", action="store_true",
        help="If an account with this email already exists, overwrite its "
             "password/name/role instead of leaving it untouched.",
    )
    parser.add_argument(
        "--must-change-password", action="store_true",
        help="Force a password change on first login (same flag every other "
             "temp-password issuance path in this codebase sets).",
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Password for the new seller account: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            sys.exit(1)
    if len(password) < 8:
        print("Refusing a password under 8 characters.", file=sys.stderr)
        sys.exit(1)

    # Idempotent — safe to run against a DB that already has tables (the
    # normal case: backend has started at least once on this deployment).
    await init_db()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == args.email))
        existing = result.scalar_one_or_none()

        if existing and not args.reset_password:
            print(f"A user with email {args.email!r} already exists (role={existing.role}).")
            if existing.role != UserRole.SELLER:
                print(
                    "  WARNING: that account is NOT a Seller-tier user "
                    f"(it's {existing.role}) — it will not be able to log "
                    "into the Seller Portal. Pick a different --email, or "
                    "pass --reset-password to overwrite it into a seller "
                    "account.", file=sys.stderr,
                )
                sys.exit(1)
            print("Nothing changed. Pass --reset-password to overwrite its password/role.")
            return

        if existing:
            existing.full_name = args.name
            existing.hashed_password = hash_password(password)
            existing.role = UserRole.SELLER
            existing.seller_role = args.role
            existing.is_active = True
            existing.is_verified = True
            existing.must_change_password = args.must_change_password
            await db.commit()
            print(f"Updated existing seller account: {args.email} (role={args.role})")
        else:
            seller = User(
                email=args.email,
                full_name=args.name,
                hashed_password=hash_password(password),
                role=UserRole.SELLER,
                seller_role=args.role,
                is_active=True,
                is_verified=True,
                must_change_password=args.must_change_password,
            )
            db.add(seller)
            await db.commit()
            print(f"Created new seller account: {args.email} (role={args.role})")

    print()
    print("Log in at the Seller Portal login page with:")
    print(f"  Email:    {args.email}")
    print(f"  Password: {'<as entered>' if not args.password else '<as passed via --password>'}")
    if args.must_change_password:
        print("  (password change will be required on first login)")


if __name__ == "__main__":
    asyncio.run(main())
