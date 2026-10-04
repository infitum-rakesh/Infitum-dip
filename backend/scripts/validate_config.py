"""
Validate a deployment's configuration before (or after) it starts serving
traffic — the real check `install.bat`/`install.sh` run at install time,
and `status.bat`/`status.sh` run for their Database row (via --db-only).

Checks performed:
  1. SECRET_KEY        — fails if still the dev default while APP_ENV=production
  2. DATABASE_URL       — real connectivity check (SELECT 1) against whatever
                          backend/mysql/postgres/sqlite DATABASE_URL points at
  3. Storage directory  — upload_dir exists (or can be created) and is writable
  4. Tesseract OCR      — real `tesseract --list-langs` call via the same
                          app.ocr.engine code path the live pipeline uses
  5. ALLOWED_ORIGINS    — not empty

(1)-(3) are blocking: a failure here means the app cannot run correctly, so
this script exits 1. (4) is a warning only — Document/Invoice Intelligence
uploads will fail without it, but a deployment that hasn't gotten to OCR
setup yet, or genuinely doesn't use those modules, can still start up; the
installer should flag it loudly rather than block on it (see
README_DEPLOYMENT.md's Tesseract section — this app never ships or installs
Tesseract itself, it only calls the real binary via pytesseract).

Usage (run from backend/):
    python scripts/validate_config.py              # all checks
    python scripts/validate_config.py --db-only     # just the DB connectivity check
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Same sys.path fixup as scripts/create_seller_account.py — running this
# file directly puts scripts/ on sys.path[0], not backend/, breaking
# `import app...` unless backend/ is inserted explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402

settings = get_settings()


class CheckResult:
    def __init__(self, name: str, ok: bool, message: str, blocking: bool = True):
        self.name = name
        self.ok = ok
        self.message = message
        self.blocking = blocking


async def check_database() -> CheckResult:
    """Deliberately builds its own short-lived engine with echo=False
    instead of importing the app's shared app.database.connection.engine —
    that one is created with echo=settings.app_debug, and this script's
    PASS/FAIL output needs to stay clean for status.bat/status.sh's
    --db-only parsing regardless of APP_DEBUG."""
    is_sqlite = settings.database_url.startswith("sqlite")
    probe_engine = create_async_engine(
        settings.database_url, echo=False, future=True,
        poolclass=NullPool if is_sqlite else None,
    )
    try:
        async with probe_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return CheckResult("Database", True, f"Connected OK ({probe_engine.url.get_backend_name()}: {probe_engine.url.render_as_string(hide_password=True)})")
    except Exception as exc:
        return CheckResult("Database", False, f"Could not connect to DATABASE_URL: {exc}")
    finally:
        await probe_engine.dispose()


def check_secret_key() -> CheckResult:
    is_default = settings.secret_key == "dev-secret-change-in-production"
    if is_default and settings.app_env == "production":
        return CheckResult(
            "Secret Key", False,
            "SECRET_KEY is still the development default while APP_ENV=production. "
            "Set a real random SECRET_KEY in backend/.env before going live — every "
            "existing JWT/session becomes forgeable otherwise.",
        )
    if is_default:
        return CheckResult("Secret Key", True, "Using the dev default (fine for APP_ENV != production).", blocking=False)
    return CheckResult("Secret Key", True, "Configured (non-default).")


def check_storage() -> CheckResult:
    upload_dir = Path(settings.upload_dir)
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        probe = upload_dir / ".validate_config_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult("Storage Directory", True, f"Writable: {upload_dir.resolve()}")
    except Exception as exc:
        return CheckResult("Storage Directory", False, f"upload_dir {upload_dir!r} is not writable: {exc}")


def check_ocr() -> CheckResult:
    try:
        from app.ocr import engine as ocr_engine
        langs = ocr_engine.get_installed_languages()
        label = ", ".join(sorted(langs)) if langs else "none"
        return CheckResult("Tesseract OCR", True, f"Tesseract binary OK — installed language pack(s): {label}", blocking=False)
    except Exception as exc:
        return CheckResult(
            "Tesseract OCR", False,
            f"{exc} Document Intelligence / Invoice Intelligence uploads will fail until this is fixed; "
            "every other feature is unaffected. See README_DEPLOYMENT.md's OCR/Tesseract section.",
            blocking=False,
        )


def check_origins() -> CheckResult:
    if not settings.origins_list:
        return CheckResult("Allowed Origins", False, "ALLOWED_ORIGINS resolved to an empty list — the frontend will be blocked by CORS.")
    return CheckResult("Allowed Origins", True, f"{', '.join(settings.origins_list)}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-only", action="store_true", help="Run only the database connectivity check")
    args = parser.parse_args()

    if args.db_only:
        results = [await check_database()]
    else:
        results = [
            check_secret_key(),
            await check_database(),
            check_storage(),
            check_ocr(),
            check_origins(),
        ]

    blocking_failure = False
    for result in results:
        if result.ok:
            status = "OK"
        elif result.blocking:
            status = "FAIL"
            blocking_failure = True
        else:
            status = "WARN"
        print(f"[{status}] {result.name}: {result.message}")

    if blocking_failure:
        print("\nvalidate_config: one or more blocking checks failed — see FAIL lines above.")
        return 1
    print("\nvalidate_config: all blocking checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
