"""A database identity must survive app restarts and change with a fresh volume."""

import uuid
from sqlalchemy import inspect, select
from app.models.server_deployment import ServerDeploymentState


def read_deployment_identity(db) -> str | None:
    if not inspect(db.get_bind()).has_table(ServerDeploymentState.__tablename__):
        return None
    return db.scalar(select(ServerDeploymentState.value).where(ServerDeploymentState.key == "deployment_id"))


def ensure_deployment_identity(db) -> str:
    current = read_deployment_identity(db)
    if current:
        return current
    value = str(uuid.uuid4())
    # Startup/import hold the database startup lock; the PK is the last guard.
    db.add(ServerDeploymentState(key="deployment_id", value=value))
    db.commit()
    return value
