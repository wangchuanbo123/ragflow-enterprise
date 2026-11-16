"""Track live answer streams independently of any API process lifetime."""

from datetime import datetime, timedelta

from sqlalchemy import select, update


def stream_lease(message_id: str):
    from app.core import config
    if config.CACHE_PROVIDER != "redis":
        return None
    from app.adapters.redis import RedisSemaphoreLease
    lease = RedisSemaphoreLease("stream:" + message_id, 1, config.RAG_LEASE_SECONDS)
    if not lease.acquire(0):
        raise RuntimeError("Answer stream already has an owner")
    return lease


def recover_abandoned_streams(db, *, grace_seconds: int = 180) -> int:
    """A new API instance must never interrupt another instance's live answer."""
    from app.adapters.redis import get_redis_client, key_prefix, redis_call
    from app.models.message import Message
    client = get_redis_client()
    cutoff = datetime.utcnow() - timedelta(seconds=grace_seconds)
    ids = list(db.scalars(select(Message.id).where(
        Message.status == "streaming", Message.created_at < cutoff,
    ).limit(500)))
    recovered = 0
    for message_id in ids:
        if redis_call(client.exists, key_prefix() + ":lease:stream:" + message_id):
            continue
        result = db.execute(update(Message).where(
            Message.id == message_id, Message.status == "streaming",
        ).values(status="interrupted", error_message="回答进程已中断，可重新提问"))
        recovered += result.rowcount
    db.commit()
    return recovered
