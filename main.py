"""
Maya Steady — FastAPI Application
Handles:
  - Facebook Messenger webhook
  - Test/simulator chat endpoint
  - Admin dashboard (Jinja2 templates)
  - Dashboard API routes
"""
import json
import asyncio
from contextlib import asynccontextmanager, suppress
import structlog
import httpx
from pathlib import Path
from uuid import UUID
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import settings
from database.dal import Database
from agent import agent, AgentError, reload_prompt, _FALLBACK_REPLY
from manager_agent import process_manager_message
from agent_config.seed_skills import SEED_SKILLS
from whatsapp_identity import normalize_whatsapp_id
from app_support.followups import (
    acquire_followup_dispatch_lock,
    dispatch_due_followups_once,
    followup_dispatcher_loop,
    release_followup_dispatch_lock,
)
from app_support.channels import build_channels_router
from app_support.dashboard import build_dashboard_router
from app_support.whatsapp import (
    WhatsAppRuntimeState,
    check_trial_gate as whatsapp_check_trial_gate,
    detect_manager_command as whatsapp_detect_manager_command,
    enqueue_message as whatsapp_enqueue_message,
    execute_manager_command as whatsapp_execute_manager_command,
    format_catchup as whatsapp_format_catchup,
    handle_as_prospect as whatsapp_handle_as_prospect,
    handle_manager as whatsapp_handle_manager,
    handle_message as whatsapp_handle_message,
    handle_unsupported as whatsapp_handle_unsupported,
    is_manager as whatsapp_is_manager,
    is_paused as whatsapp_is_paused,
    notify_handoff_admins as whatsapp_notify_handoff_admins,
    send_text_via_gateway as whatsapp_send_text,
    set_paused_memory as whatsapp_set_paused_memory,
    shadow_log_customer as whatsapp_shadow_log_customer,
    store_outbound_message as whatsapp_store_outbound_message,
)
from app_support.security import (
    build_pause_action_token,
    extract_admin_token,
    is_valid_fb_signature,
    require_admin_access,
    verify_pause_action_token,
)

_AGENT_CONFIG_DIR = Path(__file__).parent / "agent_config"

log = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_url = settings.database_url
    await Database.connect(db_url)
    schema_path = str(_BASE / "database" / "schema.sql")
    try:
        await Database.apply_schema(schema_path)
    except Exception as exc:
        log.info("schema_apply_skipped", reason=str(exc)[:120])
    try:
        await Database.seed_skills(SEED_SKILLS)
        log.info("skills_seeded")
    except Exception as exc:
        log.info("skills_seed_skipped", reason=str(exc)[:120])

    paused_eids = await Database.load_paused_external_ids()
    _wa_paused_set.clear()
    for eid in paused_eids:
        normalized = normalize_whatsapp_id(eid)
        if normalized:
            _wa_paused_set.add(normalized)

    followup_task = asyncio.create_task(_followup_dispatcher(), name="followup-dispatcher")
    app.state.followup_task = followup_task
    log.info("maya_steady_started", model=agent.model, paused_users=len(paused_eids))
    try:
        yield
    finally:
        followup_task.cancel()
        with suppress(asyncio.CancelledError):
            await followup_task
        await Database.disconnect()


app = FastAPI(title="Maya Steady", version="1.0.0", lifespan=lifespan)


async def _tg_alert(text: str) -> None:
    """Fire-and-forget Telegram message to the owner."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as exc:
        log.warning("tg_alert_failed", error=str(exc))

# Static files & templates
_BASE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_BASE / "dashboard" / "static")), name="static")
templates = Jinja2Templates(directory=str(_BASE / "dashboard" / "templates"))


async def _followup_dispatcher():
    """Background task: periodically send any due follow-up messages."""
    await followup_dispatcher_loop(_dispatch_due_followups_once)


async def _acquire_followup_dispatch_lock():
    return await acquire_followup_dispatch_lock()


async def _release_followup_dispatch_lock(conn) -> None:
    await release_followup_dispatch_lock(conn)


async def _dispatch_due_followups_once() -> None:
    """Send all due follow-ups once. Split out for testing and auditability."""
    await dispatch_due_followups_once(
        db=Database,
        acquire_lock=_acquire_followup_dispatch_lock,
        release_lock=_release_followup_dispatch_lock,
        store_outbound_message=_store_outbound_message,
        send_whatsapp=_wa_send_text,
        send_facebook=_send_fb_reply,
    )


_is_valid_fb_signature = is_valid_fb_signature


async def _handle_fb_message(sender_id: str, text: str):
    try:
        response_text = await agent.process_message(
            external_id=sender_id,
            message=text,
            channel="facebook",
        )
        await _send_fb_reply(sender_id, response_text)
    except Exception as exc:
        log.error("fb_message_handling_error", error=str(exc))
        try:
            await _store_outbound_message(sender_id, "facebook", _FALLBACK_REPLY)
            await _send_fb_reply(sender_id, _FALLBACK_REPLY)
        except Exception as send_exc:
            log.error("fb_fallback_send_failed", error=str(send_exc))


async def _send_fb_reply(recipient_id: str, text: str):
    if not settings.fb_page_access_token:
        log.warning("fb_reply_skipped_no_token")
        return
    url = "https://graph.facebook.com/v19.0/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
        "messaging_type": "RESPONSE",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, params={"access_token": settings.fb_page_access_token})
    if resp.status_code != 200:
        log.error("fb_send_failed", status=resp.status_code, body=resp.text[:200])


# ------------------------------------------------------------------ #
# WhatsApp Webhook (local gateway)
# ------------------------------------------------------------------ #

# Debounce state: sender_id -> (pending_task, accumulated_texts, display_name)
WA_DEBOUNCE_SECONDS = 15  # wait this long after last message before replying
_wa_state = WhatsAppRuntimeState()
_PROSPECT_TEST_ID = "prospect_test_internal"


def _is_paused(sender_id: str) -> bool:
    return whatsapp_is_paused(_wa_state, sender_id)


def _set_paused_memory(external_id: str, paused: bool) -> None:
    whatsapp_set_paused_memory(_wa_state, external_id, paused)


_extract_admin_token = extract_admin_token
_require_admin_access = require_admin_access
_build_pause_action_token = build_pause_action_token
_verify_pause_action_token = verify_pause_action_token

app.include_router(
    build_dashboard_router(
        templates=templates,
        db=Database,
        require_admin_access=_require_admin_access,
        build_pause_action_token=_build_pause_action_token,
        settings=settings,
    )
)


def _wa_enqueue(sender_id: str, text: str, name: Optional[str]) -> None:
    asyncio.create_task(
        whatsapp_enqueue_message(
            state=_wa_state,
            sender_id=sender_id,
            text=text,
            name=name,
            debounce_seconds=WA_DEBOUNCE_SECONDS,
            is_manager_fn=_is_manager,
            is_paused_fn=_is_paused,
            handle_message=_wa_handle_message,
            shadow_log_customer=_wa_shadow_log_customer,
        )
    )


async def _wa_shadow_log_customer(sender_id: str, text: str, name: Optional[str]) -> None:
    await whatsapp_shadow_log_customer(Database, log, sender_id, text, name)


async def _store_outbound_message(
    external_id: str,
    channel: str,
    text: str,
    conversation_id: Optional[UUID] = None,
    source: str = "maya",
) -> Optional[dict]:
    """Persist an outbound customer-facing message before transport send."""
    return await whatsapp_store_outbound_message(Database, external_id, channel, text, conversation_id, source)


def _format_catchup(shadow_messages: list) -> str:
    return whatsapp_format_catchup(shadow_messages)


async def _handle_admin_reply(recipient_jid: str, text: str) -> None:
    """Shadow-log admin replies without pausing Maya."""
    try:
        user = await Database.get_or_create_user(recipient_jid, "whatsapp")
        conv = await Database.get_active_conversation(user["id"])
        if conv:
            await Database.store_message(conv["id"], "assistant", text, source="admin")
        log.info("admin_reply_shadow_logged", recipient=recipient_jid, preview=text[:60])
    except Exception as exc:
        log.error("handle_admin_reply_error", error=str(exc))


def _is_manager(sender_id: str) -> bool:
    return whatsapp_is_manager(settings, sender_id)


def _detect_manager_command(text: str) -> Optional[str]:
    return whatsapp_detect_manager_command(text)


async def _execute_manager_command(sender_id: str, command: str, raw_text: str) -> None:
    await whatsapp_execute_manager_command(
        state=_wa_state,
        db=Database,
        agent=agent,
        send_text=_wa_send_text,
        reload_prompt=reload_prompt,
        prompt_dir=_AGENT_CONFIG_DIR,
        sender_id=sender_id,
        command=command,
        raw_text=raw_text,
        log=log,
        fallback_reply=_FALLBACK_REPLY,
        prospect_test_id=_PROSPECT_TEST_ID,
    )


async def _wa_handle_as_prospect(manager_id: str, text: str, name: Optional[str]) -> None:
    await whatsapp_handle_as_prospect(
        agent=agent,
        send_text=_wa_send_text,
        manager_id=manager_id,
        text=text,
        prospect_test_id=_PROSPECT_TEST_ID,
        log=log,
    )


async def _wa_handle_manager(sender_id: str, text: str):
    await whatsapp_handle_manager(
        process_manager_message=process_manager_message,
        send_text=_wa_send_text,
        sender_id=sender_id,
        text=text,
        log=log,
    )


async def _check_trial_gate(sender_id: str) -> Optional[str]:
    return await whatsapp_check_trial_gate(db=Database, settings=settings, log=log, sender_id=sender_id)


async def _notify_handoff_admins(external_id: str, user_id: UUID, conv_id: Optional[UUID], reason: str, priority: str, notes: Optional[str], display: str) -> None:
    await whatsapp_notify_handoff_admins(
        settings=settings,
        send_text=_wa_send_text,
        conv_id=conv_id,
        reason=reason,
        priority=priority,
        notes=notes,
        display=display,
        log=log,
    )


async def _wa_handle_message(sender_id: str, text: str, name: Optional[str]):
    await whatsapp_handle_message(
        state=_wa_state,
        db=Database,
        settings=settings,
        agent=agent,
        send_text=_wa_send_text,
        store_outbound_message_fn=_store_outbound_message,
        tg_alert=_tg_alert,
        process_manager_message=process_manager_message,
        reload_prompt=reload_prompt,
        prompt_dir=_AGENT_CONFIG_DIR,
        sender_id=sender_id,
        text=text,
        name=name,
        is_manager_fn=_is_manager,
        is_paused_fn=_is_paused,
        set_paused_memory_fn=_set_paused_memory,
        detect_manager_command_fn=_detect_manager_command,
        log=log,
        fallback_reply=_FALLBACK_REPLY,
        prospect_test_id=_PROSPECT_TEST_ID,
    )



async def _wa_handle_unsupported(sender_id: str):
    await whatsapp_handle_unsupported(
        store_outbound_message_fn=_store_outbound_message,
        send_text=_wa_send_text,
        sender_id=sender_id,
    )


_SEND_RETRIES = 3
_SEND_BACKOFF_BASE = 2.0  # seconds


async def _wa_send_text(to: str, text: str):
    await whatsapp_send_text(
        http_client_factory=httpx.AsyncClient,
        settings=settings,
        log=log,
        to=to,
        text=text,
        retries=_SEND_RETRIES,
        backoff_base=_SEND_BACKOFF_BASE,
    )


async def _process_test_chat(user_id: str, message: str, channel: str):
    return await agent.process_message(
        external_id=user_id,
        message=message,
        channel=channel,
    )


async def _get_chat_history(external_id: str):
    user = await Database.get_user_by_external_id(external_id)
    if not user:
        return {"messages": []}
    conv = await Database.get_active_conversation(user["id"])
    if not conv:
        return {"messages": []}
    history = await Database.get_conversation_history(conv["id"], limit=100)
    return {"messages": history}


_channels_router, _channel_exports = build_channels_router(
    settings=settings,
    require_admin_access=_require_admin_access,
    verify_pause_action_token=_verify_pause_action_token,
    is_valid_fb_signature=_is_valid_fb_signature,
    handle_fb_message=_handle_fb_message,
    send_fb_reply=_send_fb_reply,
    wa_enqueue=_wa_enqueue,
    handle_unsupported=_wa_handle_unsupported,
    handle_admin_reply=_handle_admin_reply,
    set_paused_memory=_set_paused_memory,
    store_outbound_message=_store_outbound_message,
    process_test_chat=_process_test_chat,
    get_chat_history=_get_chat_history,
    db=Database,
    fallback_reply=_FALLBACK_REPLY,
)
app.include_router(_channels_router)

ChatRequest = _channel_exports["ChatRequest"]
WAInternalMessage = _channel_exports["WAInternalMessage"]
AdminReplyMessage = _channel_exports["AdminReplyMessage"]
PauseRequest = _channel_exports["PauseRequest"]
fb_verify = _channel_exports["fb_verify"]
fb_webhook = _channel_exports["fb_webhook"]
wa_webhook = _channel_exports["wa_webhook"]
wa_admin_reply = _channel_exports["wa_admin_reply"]
chat = _channel_exports["chat"]
wa_internal = _channel_exports["wa_internal"]
chat_history = _channel_exports["chat_history"]


async def set_pause_by_conv(conv_id: UUID, body: PauseRequest, _: None = None):
    if not _verify_pause_action_token(conv_id, body.paused, body.token):
        raise HTTPException(status_code=403, detail="Invalid pause token")
    detail = await Database.get_conversation_detail(conv_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = detail["user_id"]
    external_id = detail["external_id"]
    _set_paused_memory(external_id, body.paused)
    await Database.set_user_paused(user_id, body.paused)
    return {"ok": True, "paused": body.paused}
