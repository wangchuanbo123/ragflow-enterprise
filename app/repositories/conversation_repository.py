"""会话与消息仓储。

所有查询都附带 user_id 过滤，保证用户隔离。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.conversation import Conversation
from app.models.message import Message


class ConversationRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_by_user(self, user_id: str) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
        )
        return list(self.db.scalars(stmt))

    def get_for_user(self, conversation_id: str, user_id: str) -> Conversation | None:
        stmt = select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
        return self.db.scalar(stmt)

    def get_detail_for_user(self, conversation_id: str, user_id: str) -> Conversation | None:
        stmt = (
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        )
        return self.db.scalar(stmt)

    def create(self, user_id: str, title: str) -> Conversation:
        conv = Conversation(user_id=user_id, title=title)
        self.db.add(conv)
        self.db.commit()
        self.db.refresh(conv)
        return conv

    def rename(self, conversation: Conversation, title: str) -> Conversation:
        conversation.title = title
        self.db.commit()
        self.db.refresh(conversation)
        return conversation

    def delete(self, conversation: Conversation) -> None:
        self.db.delete(conversation)
        self.db.commit()

    def touch(self, conversation: Conversation) -> None:
        from datetime import datetime, timezone

        conversation.last_message_at = datetime.now(timezone.utc)
        self.db.commit()


class MessageRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, message: Message) -> Message:
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        return message

    def get_for_user(
        self, message_id: str, conversation_id: str, user_id: str
    ) -> Message | None:
        stmt = (
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        return self.db.scalar(stmt)

    def list_for_conversation(
        self, conversation_id: str, user_id: str, limit: int
    ) -> list[Message]:
        stmt = (
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
                Message.status == "completed",
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        rows = list(self.db.scalars(stmt))
        rows.reverse()
        return rows

    def update(
        self,
        message: Message,
        *,
        content: str | None = None,
        status: str | None = None,
        sources_json: list | None = None,
        error_message: str | None = None,
    ) -> Message:
        if content is not None:
            message.content = content
        if status is not None:
            message.status = status
        if sources_json is not None:
            message.sources_json = sources_json
        if error_message is not None:
            message.error_message = error_message
        self.db.commit()
        self.db.refresh(message)
        return message
