"""Scheduling policy shared by the repository, execution and operations views."""
from sqlalchemy import case

RESOURCE_TYPES = {
    "parse": ("file", "sync", "rebuild", "connector_sync", "code_index"),
    "vector": ("entity_vector_rebuild", "relation_vector_rebuild", "code_vector_rebuild"),
    "model": ("graph_rebuild", "schema_generation", "schema_drift", "agent_workflow", "graph_maintenance"),
    "short": ("delete",),
}
PRIORITIES = {"delete": 0, "file": 10, "connector_sync": 20, "sync": 20,
              "schema_generation": 30, "schema_drift": 30, "agent_workflow": 30,
              "entity_vector_rebuild": 40, "relation_vector_rebuild": 40,
              "code_vector_rebuild": 40, "graph_rebuild": 50, "code_index": 50,
              "rebuild": 60, "graph_maintenance": 70}
STAGES = {"delete": "delete", "file": "document", "sync": "document", "rebuild": "document",
          "connector_sync": "sync", "code_index": "code", "entity_vector_rebuild": "vector",
          "relation_vector_rebuild": "vector", "code_vector_rebuild": "vector",
          "graph_rebuild": "graph", "schema_generation": "schema", "schema_drift": "schema",
          "agent_workflow": "agent", "graph_maintenance": "community"}


def resource_class(job_type):
    return next((name for name, kinds in RESOURCE_TYPES.items() if job_type in kinds), "parse")


def resource_expression(column):
    return case(*[(column.in_(kinds), name) for name, kinds in RESOURCE_TYPES.items()], else_="parse")


def priority_expression(column):
    return case(PRIORITIES, value=column, else_=50)


def resource_limits():
    from app.core import config
    return {name: max(1, int(getattr(config, "JOB_" + name.upper() + "_CONCURRENCY", 2)))
            for name in RESOURCE_TYPES}
