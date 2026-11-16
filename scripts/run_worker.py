"""Dedicated index/agent worker: python -m scripts.run_worker."""

import os
import signal
import argparse


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    # Select the role before application configuration or .env is loaded.
    os.environ["APP_ROLE"] = "worker"
    from app.core.init_db import init_app
    from app.core.logger import setup_logging
    from app.agents.runtime import start_managed_opencode, stop_managed_opencode
    from app.services.index_worker import run_worker_forever, stop_worker, _worker_stop

    setup_logging()
    from app.services.observability import start_metrics, stop_metrics
    start_metrics()
    init_app(start_services=False)
    _worker_stop.clear()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop_worker())
    try:
        health = start_managed_opencode()
        if health.get("status") == "fail":
            raise RuntimeError("Worker OpenCode runtime did not become ready")
        run_worker_forever()
    finally:
        stop_worker()
        stop_managed_opencode()
        stop_metrics()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
