"""Exercise the built production image against explicitly supplied test services.

Requires a disposable database: migrations, administrator and KB are created.
No inference request is sent. Run with APP_PROFILE=server, APP_ROLE=api.
"""
import os


def main():
    if os.getenv("IMAGE_SMOKE_DISPOSABLE") != "true":
        raise RuntimeError("IMAGE_SMOKE_DISPOSABLE=true is required")
    from app.core import config
    if config.APP_PROFILE != "server" or config.APP_ROLE != "api":
        raise RuntimeError("Image smoke requires server API profile")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        response = client.post("/api/v1/auth/login", json={
            "username": config.INITIAL_ADMIN_USERNAME,
            "password": config.INITIAL_ADMIN_PASSWORD,
        })
        assert response.status_code == 200, "image login failed"
        assert client.get("/api/v1/knowledge-bases").status_code == 200
        assert client.get("/api/v1/operations/metrics").status_code == 200
        assert client.post("/api/v1/operations/index-audit").status_code == 200
        assert client.get("/").status_code == 200, "built frontend missing"
    print("Production image smoke passed: migrations, login, metadata, metrics, index audit, frontend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
