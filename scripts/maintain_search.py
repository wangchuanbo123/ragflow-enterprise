"""Operator-only search maintenance, safe dry run by default.

Run inside the configured server environment. The operator must select an
existing active administrator. No endpoint exposes this DDL capability.
"""

import argparse
import json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["hnsw", "validate-hnsw", "warm"])
    parser.add_argument("--knowledge-base", required=True)
    parser.add_argument("--admin-username", required=True)
    parser.add_argument("--collection", help="Explicit versioned vector collection; required for hnsw")
    parser.add_argument("--apply", action="store_true", help="Perform maintenance; otherwise only describe the plan")
    parser.add_argument("--min-rows", type=int, default=100000)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--min-recall", type=float, default=0.95)
    parser.add_argument("--max-p95-ms", type=float, default=1000)
    args = parser.parse_args(argv)
    from sqlalchemy import select
    from app.core.database import SessionLocal, engine
    from app.models.user import User
    from app.models.knowledge_base import KnowledgeBase
    from rag.providers.interfaces import VectorIndexRef
    from rag.providers.pgvector_store import PgVectorStoreProvider
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.admin_username, User.is_active.is_(True), User.role == "admin"))
        if user is None or db.get(KnowledgeBase, args.knowledge_base) is None:
            parser.error("Select an existing active administrator and knowledge base")
    if args.action in {"hnsw", "validate-hnsw"}:
        if not args.collection:
            parser.error("--collection is required for hnsw maintenance")
        provider = PgVectorStoreProvider(engine)
        ref = VectorIndexRef(args.collection, args.knowledge_base)
        result = (provider.maintain_search_index(ref, apply=args.apply, min_rows=args.min_rows)
            if args.action == "hnsw" else provider.validate_search_index(ref, apply=args.apply,
                samples=args.samples, min_recall=args.min_recall, max_p95_ms=args.max_p95_ms))
    elif args.apply:
        from rag.retrievers.bm25_retriever import get_bm25_retriever
        retriever = get_bm25_retriever(args.knowledge_base)
        retriever.build()
        result = {"status": "ready", **retriever.build_stats}
    else:
        result = {"status": "planned", "knowledge_base_id": args.knowledge_base, "action": "warm"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
