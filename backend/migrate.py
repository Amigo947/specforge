"""
Pre-migration bootstrap script.

When the DB was previously created with SQLAlchemy's create_all (no alembic),
there is no alembic_version table. Running `alembic upgrade head` would try to
re-create tables that already exist and fail.

This script detects that situation, stamps the DB at the correct baseline
revision (so alembic knows what has already been applied), then runs upgrade.
"""
import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


async def _detect_baseline() -> str | None:
    """Return the revision to stamp, or None if alembic_version already exists."""
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            # Is alembic already tracking this DB?
            row = await conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'alembic_version'"
            ))
            if row.scalar():
                return None  # Already managed by alembic — nothing to stamp

            # DB exists but has no alembic history; figure out how far along it is.
            row = await conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE()"
            ))
            tables = {r[0] for r in row}

            if "users" not in tables:
                # Only migration 001 (or nothing) applied
                return "001_initial" if "projects" in tables else None

            # Both projects + users exist — check for user_id column (migration 003)
            row = await conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'projects' AND column_name = 'user_id'"
            ))
            if row.scalar():
                return "003_add_user_id_to_projects"  # Fully up-to-date

            return "002_add_users"  # Need to run 003
    finally:
        await engine.dispose()


def main() -> None:
    baseline = asyncio.run(_detect_baseline())
    cfg = Config("alembic.ini")

    if baseline:
        print(f"[migrate] No alembic history found — stamping at {baseline}")
        command.stamp(cfg, baseline)

    print("[migrate] Running alembic upgrade head …")
    command.upgrade(cfg, "head")
    print("[migrate] Database is up to date.")


if __name__ == "__main__":
    main()
