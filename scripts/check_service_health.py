"""Container-local Worker/Scheduler heartbeat healthcheck; no public ports."""
import socket


def main():
    from app.core import config
    from app.adapters.redis import active_workers, get_redis_client, key_prefix
    if config.APP_ROLE == "worker":
        prefix = socket.gethostname() + ":"
        return 0 if any(str(item.get("worker_id", "")).startswith(prefix) and not item.get("stopping")
                        for item in active_workers()) else 1
    if config.APP_ROLE == "scheduler":
        return 0 if get_redis_client().get(key_prefix() + ":scheduler:heartbeat") else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
