"""Read local operations and emit JSON; nonzero means actionable alerts.

Run inside the API container so the existing private configuration is used:
  docker compose --env-file .env.server -f deploy/compose.yml exec -T api python -m scripts.check_operations
This CLI prints aggregate metrics only, never credentials or document content.
"""
import json


def main():
    from app.core import database
    from app.services.observability import operations_snapshot
    with database.SessionLocal() as db:
        result = operations_snapshot(db)
    print(json.dumps(result, ensure_ascii=True))
    return 2 if result["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
