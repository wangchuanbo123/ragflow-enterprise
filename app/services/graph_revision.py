"""Database triggers observe every graph writer, including bulk SQL and imports.

The counter is committed in the same transaction as its facts. Readers take it
inside their repeatable-read snapshot, so an in-flight writer cannot invalidate
the correspondence between a version and the facts stored in a cache entry.
"""

from sqlalchemy import inspect, select


GRAPH_TABLES = ("knowledge_documents", "knowledge_chunks", "kg_entities", "kg_aliases",
                "kg_entity_mentions", "kg_relations", "kg_relation_evidence", "kg_graph_corrections",
                "kg_entity_merges", "graph_schema_definitions")


def install_revision_triggers(connection):
    names = set(inspect(connection).get_table_names())
    if "knowledge_graph_revisions" not in names:
        return
    tables = [name for name in GRAPH_TABLES if name in names]
    quote = connection.dialect.identifier_preparer.quote
    if connection.dialect.name == "postgresql":
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
        revisions = f"{quote(schema)}.knowledge_graph_revisions"
        bases = f"{quote(schema)}.knowledge_bases"
        function = f"{quote(schema)}.bump_knowledge_graph_revision"
        connection.exec_driver_sql(f"""CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'TRUNCATE' THEN
            INSERT INTO {revisions}(knowledge_base_id, incarnation, revision)
              SELECT id, md5(random()::text || clock_timestamp()::text), 1 FROM {bases}
              ON CONFLICT (knowledge_base_id) DO UPDATE SET revision = {revisions}.revision + 1;
            RETURN NULL;
          END IF;
          IF TG_OP = 'INSERT' THEN
            INSERT INTO {revisions}(knowledge_base_id, incarnation, revision)
              SELECT changed.knowledge_base_id, md5(random()::text || clock_timestamp()::text), 1
              FROM (SELECT DISTINCT knowledge_base_id FROM new_graph_rows) changed
              WHERE EXISTS (SELECT 1 FROM {bases} WHERE id = changed.knowledge_base_id)
              ON CONFLICT (knowledge_base_id) DO UPDATE SET revision = {revisions}.revision + 1;
          ELSIF TG_OP = 'DELETE' THEN
            INSERT INTO {revisions}(knowledge_base_id, incarnation, revision)
              SELECT changed.knowledge_base_id, md5(random()::text || clock_timestamp()::text), 1
              FROM (SELECT DISTINCT knowledge_base_id FROM old_graph_rows) changed
              WHERE EXISTS (SELECT 1 FROM {bases} WHERE id = changed.knowledge_base_id)
              ON CONFLICT (knowledge_base_id) DO UPDATE SET revision = {revisions}.revision + 1;
          ELSE
            INSERT INTO {revisions}(knowledge_base_id, incarnation, revision)
              SELECT changed.knowledge_base_id, md5(random()::text || clock_timestamp()::text), 1
              FROM (SELECT knowledge_base_id FROM old_graph_rows UNION SELECT knowledge_base_id FROM new_graph_rows) changed
              WHERE EXISTS (SELECT 1 FROM {bases} WHERE id = changed.knowledge_base_id)
              ON CONFLICT (knowledge_base_id) DO UPDATE SET revision = {revisions}.revision + 1;
          END IF;
          RETURN NULL;
        END $$""")
        for name in tables:
            qualified = f"{quote(schema)}.{quote(name)}"
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS graph_revision_changed ON {qualified}")
            # One counter bump per affected KB/statement, even for a bulk write
            # of thousands of chunks. A hot counter is never updated per row.
            transitions = {"insert": "NEW TABLE AS new_graph_rows", "delete": "OLD TABLE AS old_graph_rows",
                           "update": "OLD TABLE AS old_graph_rows NEW TABLE AS new_graph_rows"}
            for action, tables_clause in transitions.items():
                trigger = f"graph_revision_{action}"
                connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger} ON {qualified}")
                connection.exec_driver_sql(f"CREATE TRIGGER {trigger} AFTER {action.upper()} ON {qualified} "
                    f"REFERENCING {tables_clause} FOR EACH STATEMENT EXECUTE FUNCTION {function}()")
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS graph_revision_truncated ON {qualified}")
            connection.exec_driver_sql(f"CREATE TRIGGER graph_revision_truncated AFTER TRUNCATE ON {qualified} FOR EACH STATEMENT EXECUTE FUNCTION {function}()")
    elif connection.dialect.name == "sqlite":
        for name in tables:
            for action, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                trigger = quote(f"graph_revision_{name}_{action.lower()}")
                connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {action} ON {quote(name)} BEGIN
                INSERT INTO knowledge_graph_revisions(knowledge_base_id, incarnation, revision)
                  SELECT {ref}.knowledge_base_id, lower(hex(randomblob(16))), 1
                  WHERE EXISTS (SELECT 1 FROM knowledge_bases WHERE id = {ref}.knowledge_base_id)
                  ON CONFLICT(knowledge_base_id) DO UPDATE SET revision = revision + 1;
                END""")
            connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS {quote(f'graph_revision_{name}_move')} AFTER UPDATE ON {quote(name)}
                WHEN NEW.knowledge_base_id <> OLD.knowledge_base_id BEGIN
                INSERT INTO knowledge_graph_revisions(knowledge_base_id, incarnation, revision)
                  SELECT OLD.knowledge_base_id, lower(hex(randomblob(16))), 1 WHERE EXISTS (SELECT 1 FROM knowledge_bases WHERE id=OLD.knowledge_base_id)
                  ON CONFLICT(knowledge_base_id) DO UPDATE SET revision=revision+1; END""")


def current_revision(db, knowledge_base_id):
    from app.models.graph_revision import KnowledgeGraphRevision
    row = db.execute(select(KnowledgeGraphRevision.incarnation, KnowledgeGraphRevision.revision)
                     .where(KnowledgeGraphRevision.knowledge_base_id == knowledge_base_id)).first()
    return tuple(row) if row is not None else ("empty", 0)
