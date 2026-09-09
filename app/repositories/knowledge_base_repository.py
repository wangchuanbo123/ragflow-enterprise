"""Knowledge-base metadata and membership repository."""

from __future__ import annotations

import re
import uuid

from datetime import datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseMember,
    KnowledgeDataSource,
)
from app.models.graph_governance import GraphSchemaDefinition
from app.models.user import User
from app.core.config import GRAPH_SCHEMA_VERSION
from rag.knowledge_graph.schemas import VALID_ENTITY_TYPES, VALID_PREDICATES

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


class KnowledgeBaseRepository:
    def __init__(self, db: Session):
        self.db = db

    def _ensure_active_schema(self, knowledge_base_id: str) -> None:
        existing = self.db.scalar(select(GraphSchemaDefinition.id).where(
            GraphSchemaDefinition.knowledge_base_id == knowledge_base_id,
            GraphSchemaDefinition.graph_kind == "general",
            GraphSchemaDefinition.status == "active",
        ).limit(1))
        if existing is not None:
            return
        latest = self.db.scalar(select(func.max(GraphSchemaDefinition.version)).where(
            GraphSchemaDefinition.knowledge_base_id == knowledge_base_id
        )) or 0
        self.db.add(GraphSchemaDefinition(
            knowledge_base_id=knowledge_base_id,
            graph_kind="general",
            version=max(int(latest) + 1, GRAPH_SCHEMA_VERSION),
            status="active",
            entity_types_json=sorted(VALID_ENTITY_TYPES),
            relation_types_json=sorted(VALID_PREDICATES),
            graph_profiles_json=None,
            extraction_prompt=None,
            normalization_rules_json={
                "unicode": "NFKC",
                "case": "lower_ascii",
                "trim_punctuation": True,
            },
            activated_at=datetime.utcnow(),
        ))

    def ensure_default(self, owner_id: str | None = None) -> KnowledgeBase:
        knowledge_base = self.db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID)
        if knowledge_base is None:
            knowledge_base = KnowledgeBase.default()
            knowledge_base.owner_id = owner_id
            self.db.add(knowledge_base)
            self.db.flush()
        elif owner_id and knowledge_base.owner_id is None:
            knowledge_base.owner_id = owner_id
        if owner_id and self.member_role(DEFAULT_KNOWLEDGE_BASE_ID, owner_id) is None:
            self.db.add(
                KnowledgeBaseMember(
                    knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                    user_id=owner_id,
                    role="owner",
                )
            )
        self._ensure_active_schema(DEFAULT_KNOWLEDGE_BASE_ID)
        self.db.commit()
        self.db.refresh(knowledge_base)
        return knowledge_base

    def get(self, knowledge_base_id: str) -> KnowledgeBase | None:
        return self.db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.is_active.is_(True),
            )
        )

    def list_for_user(self, user_id: str, *, is_admin: bool = False) -> list[KnowledgeBase]:
        stmt = select(KnowledgeBase).where(KnowledgeBase.is_active.is_(True))
        if not is_admin:
            stmt = (
                stmt.join(
                    KnowledgeBaseMember,
                    KnowledgeBaseMember.knowledge_base_id == KnowledgeBase.id,
                )
                .where(KnowledgeBaseMember.user_id == user_id)
            )
        return list(self.db.scalars(stmt.order_by(KnowledgeBase.created_at, KnowledgeBase.name)))

    def member_role(self, knowledge_base_id: str, user_id: str) -> str | None:
        return self.db.scalar(
            select(KnowledgeBaseMember.role).where(
                KnowledgeBaseMember.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseMember.user_id == user_id,
            )
        )

    def can_read(self, knowledge_base_id: str, user_id: str, *, is_admin: bool = False) -> bool:
        if is_admin:
            return self.get(knowledge_base_id) is not None
        return self.member_role(knowledge_base_id, user_id) is not None

    def can_edit(self, knowledge_base_id: str, user_id: str, *, is_admin: bool = False) -> bool:
        if is_admin:
            return self.get(knowledge_base_id) is not None
        return self.member_role(knowledge_base_id, user_id) in {"owner", "editor"}

    def _available_slug(self, name: str, requested_slug: str | None = None) -> str:
        candidate = (requested_slug or "").strip().lower()
        if not _SLUG_PATTERN.fullmatch(candidate):
            candidate = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
            if len(candidate) < 3:
                candidate = f"kb-{candidate}" if candidate else f"kb-{uuid.uuid4().hex[:8]}"
            candidate = candidate[:64].strip("-")
        base = candidate
        counter = 2
        while self.db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.slug == candidate)):
            suffix = f"-{counter}"
            candidate = f"{base[:64-len(suffix)].rstrip('-')}{suffix}"
            counter += 1
        return candidate

    def create(self, *, name: str, slug: str | None = None, owner_id: str) -> KnowledgeBase:
        normalized_slug = self._available_slug(name, slug)
        duplicate = self.db.scalar(
            select(KnowledgeBase.id).where(KnowledgeBase.name == name.strip())
        )
        if duplicate:
            raise ValueError("知识库名称已经存在")

        knowledge_base = KnowledgeBase(
            name=name.strip(),
            slug=normalized_slug,
            owner_id=owner_id,
            is_default=False,
            is_active=True,
        )
        self.db.add(knowledge_base)
        self.db.flush()
        self.db.add(
            KnowledgeBaseMember(
                knowledge_base_id=knowledge_base.id,
                user_id=owner_id,
                role="owner",
            )
        )
        self._ensure_active_schema(knowledge_base.id)
        self.db.commit()
        self.db.refresh(knowledge_base)
        return knowledge_base

    def update(
        self,
        knowledge_base: KnowledgeBase,
        *,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        description_supplied: bool = False,
    ) -> KnowledgeBase:
        normalized_name = name.strip() if name is not None else knowledge_base.name
        normalized_slug = slug.strip().lower() if slug is not None else knowledge_base.slug
        if not _SLUG_PATTERN.fullmatch(normalized_slug):
            raise ValueError("知识库标识需要使用 3-64 位小写字母、数字或连字符")
        duplicate = self.db.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id != knowledge_base.id,
                or_(KnowledgeBase.slug == normalized_slug, KnowledgeBase.name == normalized_name),
            )
        )
        if duplicate:
            raise ValueError("知识库名称或标识已经存在")
        knowledge_base.name = normalized_name
        knowledge_base.slug = normalized_slug
        if description_supplied:
            knowledge_base.description = description.strip() if description else None
        self.db.commit()
        self.db.refresh(knowledge_base)
        return knowledge_base

    def archive(self, knowledge_base: KnowledgeBase) -> None:
        if knowledge_base.is_default:
            raise ValueError("默认知识库不能归档")
        knowledge_base.is_active = False
        self.db.commit()

    def list_members(self, knowledge_base_id: str) -> list[dict]:
        rows = self.db.execute(
            select(KnowledgeBaseMember, User).join(
                User,
                User.id == KnowledgeBaseMember.user_id,
            ).where(KnowledgeBaseMember.knowledge_base_id == knowledge_base_id)
        ).all()
        return [
            {
                "id": member.id,
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": member.role,
                "created_at": member.created_at,
            }
            for member, user in rows
        ]

    def upsert_member(
        self,
        knowledge_base_id: str,
        username: str,
        role: str,
    ) -> dict:
        user = self.db.scalar(
            select(User).where(
                User.username == username.strip(),
                User.is_active.is_(True),
            )
        )
        if user is None:
            raise ValueError("用户不存在或已停用")
        member = self.db.scalar(
            select(KnowledgeBaseMember).where(
                KnowledgeBaseMember.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseMember.user_id == user.id,
            )
        )
        if member is not None and member.role == "owner":
            raise ValueError("不能通过成员接口修改知识库所有者")
        if member is None:
            member = KnowledgeBaseMember(
                knowledge_base_id=knowledge_base_id,
                user_id=user.id,
                role=role,
            )
            self.db.add(member)
        else:
            member.role = role
        self.db.commit()
        self.db.refresh(member)
        return {
            "id": member.id,
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": member.role,
            "created_at": member.created_at,
        }

    def remove_member(self, knowledge_base_id: str, user_id: str) -> bool:
        member = self.db.scalar(
            select(KnowledgeBaseMember).where(
                KnowledgeBaseMember.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseMember.user_id == user_id,
            )
        )
        if member is None:
            return False
        if member.role == "owner":
            raise ValueError("不能移除知识库所有者")
        self.db.delete(member)
        self.db.commit()
        return True

    def list_data_sources(self, knowledge_base_id: str) -> list[KnowledgeDataSource]:
        return list(self.db.scalars(
            select(KnowledgeDataSource)
            .where(KnowledgeDataSource.knowledge_base_id == knowledge_base_id)
            .order_by(KnowledgeDataSource.created_at.desc())
        ))

    def get_data_source(
        self,
        knowledge_base_id: str,
        source_id: str,
    ) -> KnowledgeDataSource | None:
        return self.db.scalar(
            select(KnowledgeDataSource).where(
                KnowledgeDataSource.id == source_id,
                KnowledgeDataSource.knowledge_base_id == knowledge_base_id,
            )
        )

    def create_data_source(
        self,
        knowledge_base_id: str,
        *,
        source_type: str,
        name: str,
        connection_ref: str | None,
        config: dict,
        sync_interval_minutes: int | None = None,
        deletion_policy: str = "ignore",
    ) -> KnowledgeDataSource:
        duplicate = self.db.scalar(
            select(KnowledgeDataSource.id).where(
                KnowledgeDataSource.knowledge_base_id == knowledge_base_id,
                KnowledgeDataSource.name == name.strip(),
            )
        )
        if duplicate:
            raise ValueError("数据源名称已存在")
        source = KnowledgeDataSource(
            knowledge_base_id=knowledge_base_id,
            source_type=source_type,
            name=name.strip(),
            connection_ref=connection_ref,
            config_json=config,
            status="active",
            sync_interval_minutes=sync_interval_minutes,
            deletion_policy=deletion_policy,
        )
        self.db.add(source)
        self.db.commit()
        self.db.refresh(source)
        return source

    def list_due_data_sources(self, now: datetime, limit: int = 100) -> list[KnowledgeDataSource]:
        return list(self.db.scalars(
            select(KnowledgeDataSource)
            .where(
                KnowledgeDataSource.status == "active",
                KnowledgeDataSource.sync_interval_minutes.is_not(None),
                or_(
                    KnowledgeDataSource.next_sync_at.is_(None),
                    KnowledgeDataSource.next_sync_at <= now,
                ),
            )
            .order_by(KnowledgeDataSource.next_sync_at, KnowledgeDataSource.created_at)
            .limit(limit)
        ))

    def update_data_source(
        self,
        knowledge_base_id: str,
        source_id: str,
        **changes,
    ) -> KnowledgeDataSource | None:
        source = self.get_data_source(knowledge_base_id, source_id)
        if source is None:
            return None
        for key, value in changes.items():
            if key == "config":
                source.config_json = value
            elif key == "connection_ref":
                source.connection_ref = value
            elif key == "sync_interval_minutes":
                source.sync_interval_minutes = value
                source.next_sync_at = None
            elif value is not None:
                setattr(source, key, value.strip() if key == "name" else value)
        self.db.commit()
        self.db.refresh(source)
        return source

    def delete_data_source(self, knowledge_base_id: str, source_id: str) -> bool:
        result = self.db.execute(
            delete(KnowledgeDataSource).where(
                KnowledgeDataSource.id == source_id,
                KnowledgeDataSource.knowledge_base_id == knowledge_base_id,
            )
        )
        self.db.commit()
        return bool(result.rowcount)
