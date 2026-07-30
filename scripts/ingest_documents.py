"""文档索引管理脚本。

支持命令：
  --dry-run              查看同步变化但不执行
  --sync                 增量同步
  --rebuild --yes        全量影子 collection 重建
  --list-collections     列出所有 collection
  --rollback-collection <name> --yes   回滚到指定 collection
  --cleanup-collection <name> --yes    删除非活动 collection
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.config import DOC_DIR
from app.core.database import SessionLocal


def cmd_dry_run():
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        mgr = IndexManager(db)
        result = mgr.sync(DOC_DIR, dry_run=True)

    print("\n=== Dry Run ===")
    classification = result.get("classification", {})
    for action in ("added", "modified", "deleted", "unchanged"):
        items = classification.get(action, [])
        print(f"{action}: {len(items)}")
        for item in items[:10]:
            print(f"  {item['source_path']}")

    if result.get("will_rebuild"):
        print("\nNote: No active collection, --sync will trigger full rebuild.")


def cmd_sync():
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        mgr = IndexManager(db)
        result = mgr.sync(DOC_DIR, dry_run=False)

    print("\n=== Sync Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_rebuild():
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        mgr = IndexManager(db)
        result = mgr.rebuild(DOC_DIR, dry_run=False)

    print("\n=== Rebuild Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_list_collections():
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        mgr = IndexManager(db)
        collections = mgr.list_collections()

    print("\n=== Collections ===")
    for coll in collections:
        marker = " [ACTIVE]" if coll["is_active"] else ""
        print(f"  {coll['name']}: {coll['count']} vectors{marker}")


def cmd_rollback(name: str):
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        mgr = IndexManager(db)
        result = mgr.rollback_collection(name)

    print("\n=== Rollback Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_cleanup(name: str):
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        mgr = IndexManager(db)
        result = mgr.cleanup_collection(name)

    print("\n=== Cleanup Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_graph_only():
    """对已有 Chunk 构建知识图谱，不重建文档向量。"""
    from app.services.knowledge_graph_service import GraphBuildService

    with SessionLocal() as db:
        svc = GraphBuildService(db)
        result = svc.build_graph(only_pending=True)

    print("\n=== Graph Build Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_retry_failed():
    """重试图谱抽取失败的片段。"""
    from app.services.knowledge_graph_service import GraphBuildService

    with SessionLocal() as db:
        svc = GraphBuildService(db)
        result = svc.build_graph(only_pending=False, retry_failed=True)

    print("\n=== Graph Retry Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    from app.core.init_db import run_migrations

    run_migrations()
    parser = argparse.ArgumentParser(description="文档索引管理")
    parser.add_argument("--dry-run", action="store_true", help="查看同步变化")
    parser.add_argument("--sync", action="store_true", help="增量同步")
    parser.add_argument("--rebuild", action="store_true", help="全量影子重建")
    parser.add_argument("--graph-only", action="store_true", help="只处理图谱抽取")
    parser.add_argument("--retry-failed", action="store_true", help="重试失败的图谱抽取")
    parser.add_argument("--file", type=str, default=None, help="同步单个文件")
    parser.add_argument("--yes", action="store_true", help="确认执行")
    parser.add_argument("--list-collections", action="store_true", help="列出 collections")
    parser.add_argument("--rollback-collection", type=str, default=None)
    parser.add_argument("--cleanup-collection", type=str, default=None)
    args = parser.parse_args()

    if args.list_collections:
        cmd_list_collections()
        return

    if args.rollback_collection:
        if not args.yes:
            print("Use --yes to confirm rollback.")
            return
        cmd_rollback(args.rollback_collection)
        return

    if args.cleanup_collection:
        if not args.yes:
            print("Use --yes to confirm cleanup.")
            return
        cmd_cleanup(args.cleanup_collection)
        return

    if args.dry_run:
        cmd_dry_run()
        return

    if args.rebuild:
        if not args.yes:
            print("Use --yes to confirm rebuild.")
            return
        cmd_rebuild()
        return

    if args.sync:
        cmd_sync()
        return

    if args.graph_only:
        cmd_graph_only()
        return

    if args.retry_failed:
        cmd_retry_failed()
        return

    # Default: dry-run
    cmd_dry_run()


if __name__ == "__main__":
    main()
