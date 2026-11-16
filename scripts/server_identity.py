"""Read/initialize the database identity without printing connection settings."""

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--read", action="store_true")
    group.add_argument("--ensure", action="store_true")
    args = parser.parse_args()
    from app.core.database import SessionLocal
    from app.core.init_db import database_startup_lock, run_migrations
    from app.services.deployment_identity import read_deployment_identity, ensure_deployment_identity
    if args.read:
        with SessionLocal() as db:
            value = read_deployment_identity(db)
    else:
        from sqlalchemy.orm import Session
        with database_startup_lock() as connection:
            run_migrations(connection=connection)
            with Session(bind=connection) as db:
                value = ensure_deployment_identity(db)
    print(value or "missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
