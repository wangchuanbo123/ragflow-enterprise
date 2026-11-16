"""Plan or queue a full KB shadow rebuild from its registered originals.

Run inside the configured API container. Dry run is the default; --apply only
queues a normal Worker job and never rebuilds vectors in the CLI process.
"""

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base", required=True)
    parser.add_argument("--admin-username", required=True)
    parser.add_argument("--apply", action="store_true", help="Queue the rebuild; otherwise inspect originals only")
    args = parser.parse_args(argv)

    from sqlalchemy import select
    from app.adapters.local import LocalObjectStore
    from app.core.config import LOCAL_OBJECT_STORE_DIR
    from app.core.database import SessionLocal
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User
    from app.services.index_rebuild_service import inspect_rebuild_originals

    with SessionLocal() as db:
        administrator = db.scalar(select(User).where(
            User.username == args.admin_username, User.is_active.is_(True), User.role == "admin",
        ))
        knowledge_base = db.get(KnowledgeBase, args.knowledge_base)
        if administrator is None or knowledge_base is None or not knowledge_base.is_active:
            parser.error("Select an existing active administrator and active knowledge base")
        if not Path(LOCAL_OBJECT_STORE_DIR).is_dir():
            parser.error("The configured original-object directory is unavailable")
        object_store = LocalObjectStore(LOCAL_OBJECT_STORE_DIR)
        result = inspect_rebuild_originals(db, args.knowledge_base, object_store)
        if args.apply and result["ready"]:
            from app.services.document_index_service import DocumentIndexService

            job = DocumentIndexService(db, args.knowledge_base, object_store=object_store).request_rebuild_job(administrator.id)
            result.update(status="accepted", job_id=job.id, job_type=job.job_type)
        else:
            result.update(status="planned" if result["ready"] else "blocked", dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
