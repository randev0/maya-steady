import asyncio
from typing import Awaitable, Callable, Optional

import structlog

from config import settings
from database.dal import Database

log = structlog.get_logger()

_FOLLOWUP_DISPATCH_LOCK_KEY = 18420815


async def acquire_followup_dispatch_lock():
    if not Database.pool:
        return None
    conn = await Database.pool.acquire()
    try:
        acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _FOLLOWUP_DISPATCH_LOCK_KEY)
        if not acquired:
            await Database.pool.release(conn)
            return None
        return conn
    except Exception:
        await Database.pool.release(conn)
        raise


async def release_followup_dispatch_lock(conn) -> None:
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", _FOLLOWUP_DISPATCH_LOCK_KEY)
    finally:
        await Database.pool.release(conn)


async def dispatch_due_followups_once(
    *,
    db,
    acquire_lock: Callable[[], Awaitable[object]],
    release_lock: Callable[[object], Awaitable[None]],
    store_outbound_message: Callable[..., Awaitable[dict]],
    send_whatsapp: Callable[[str, str], Awaitable[None]],
    send_facebook: Callable[[str, str], Awaitable[None]],
) -> None:
    lock_conn = await acquire_lock()
    if lock_conn is None:
        log.debug("followup_dispatcher_lock_skipped")
        return
    try:
        due = await db.get_due_followups()
        for fu in due:
            try:
                if not fu.get("message_id"):
                    stored = await store_outbound_message(
                        external_id=fu["external_id"],
                        channel=fu["channel"],
                        text=fu["message"],
                        conversation_id=fu.get("conversation_id"),
                        source="follow_up",
                    )
                    await db.attach_followup_message(fu["id"], stored["id"])
                channel = fu["channel"]
                if channel == "whatsapp":
                    await send_whatsapp(fu["external_id"], fu["message"])
                elif channel == "facebook":
                    await send_facebook(fu["external_id"], fu["message"])
                await db.mark_followup_sent(fu["id"])
                await db.record_tool_outcome(
                    tool_name="follow_up_dispatcher",
                    success=True,
                    reason=fu["follow_up_type"],
                    details={"follow_up_id": str(fu["id"]), "channel": channel},
                    user_id=fu["user_id"],
                    conversation_id=fu.get("conversation_id"),
                )
                log.info("followup_sent", type=fu["follow_up_type"], user=fu["external_id"])
            except Exception as exc:
                await db.record_tool_outcome(
                    tool_name="follow_up_dispatcher",
                    success=False,
                    reason=str(exc),
                    details={"follow_up_id": str(fu["id"]), "channel": fu["channel"]},
                    user_id=fu["user_id"],
                    conversation_id=fu.get("conversation_id"),
                )
                log.error("followup_send_failed", id=str(fu["id"]), error=str(exc))
    except Exception as exc:
        log.error("followup_dispatcher_error", error=str(exc))
    finally:
        await release_lock(lock_conn)


async def followup_dispatcher_loop(dispatch_once: Callable[[], Awaitable[None]]) -> None:
    while True:
        await asyncio.sleep(settings.followup_dispatch_interval_seconds)
        await dispatch_once()
