"""Initialize schema and administrator for a new empty deployment."""

from __future__ import annotations


def main() -> int:
    from app.core.database import SessionLocal
    from app.core.init_db import ensure_initial_admin, run_migrations
    from app.models.user import User
    from app.repositories.knowledge_base_repository import KnowledgeBaseRepository

    run_migrations()
    with SessionLocal() as db:
        ensure_initial_admin(db)
        admin = db.query(User).filter(User.role == "admin").order_by(User.created_at).first()
        KnowledgeBaseRepository(db).ensure_default(admin.id if admin else None)
    print("empty environment initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
