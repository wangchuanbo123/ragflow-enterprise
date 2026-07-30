"""日志配置。"""

import logging
import sys


def setup_logging():
    """配置根日志格式。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(handler)

    # Alembic emits routine SQLite and plugin setup details at INFO level.
    # Keep warnings and migration failures visible without cluttering startup.
    logging.getLogger("alembic").setLevel(logging.WARNING)


setup_logging()
