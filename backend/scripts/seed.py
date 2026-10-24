"""Seed reference and/or demonstration data.

    python -m scripts.seed --reference            # real reference catalogue shown by the frontend
    python -m scripts.seed --demo                 # SAMPLE editions/stories (refused in production)
    python -m scripts.seed --reference --demo --dry-run

Safe to run repeatedly: existing rows are skipped, never updated or deleted.
"""

import argparse
import sys

from app.core.config import Settings, describe_database_target
from app.db.session import Database
from app.seed.runner import SeedRefused, check_allowed, run_seed


class _DryRun(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference", action="store_true", help="Insert the public reference catalogue.")
    parser.add_argument("--demo", action="store_true", help="Insert clearly labelled demo content (non-production).")
    parser.add_argument("--dry-run", action="store_true", help="Run inside a transaction, then roll it back.")
    parser.add_argument("--confirm-production", action="store_true", help="Required for --reference in production.")
    args = parser.parse_args(argv)

    settings = Settings()
    try:
        check_allowed(settings, reference=args.reference, demo=args.demo, confirm_production=args.confirm_production)
    except SeedRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    if settings.database_url is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2

    groups = " + ".join(g for g, on in (("reference", args.reference), ("demo", args.demo)) if on)
    print(f"Seeding {groups} into {describe_database_target(settings)} (APP_ENV={settings.app_env.value})")
    db = Database(settings.model_copy(update={"db_use_null_pool": True}))
    try:
        with db.write_session() as session:
            report = run_seed(session, reference=args.reference, demo=args.demo)
            if args.dry_run:
                raise _DryRun
    except _DryRun:
        print("Dry run: rolled back.")
    finally:
        db.dispose()

    for table in sorted(set(report.inserted) | set(report.skipped)):
        print(f"  {table:<16} inserted {report.inserted.get(table, 0):>3}  skipped {report.skipped.get(table, 0):>3}")
    if args.demo and not settings.expose_demo_content:
        print("Note: demo rows stay hidden from the API until EXPOSE_DEMO_CONTENT=true (non-production only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
