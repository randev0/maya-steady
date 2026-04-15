"""
LeadQualBot — FastAPI Application
Handles:
  - Facebook Messenger webhook
  - Test/simulator chat endpoint
  - Admin dashboard (Jinja2 templates)
  - Dashboard API routes
"""
import json
import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager, suppress
import structlog
import httpx
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query, Depends, Header
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import settings
from database.dal import Database
from agent import agent, AgentError, reload_prompt, _FALLBACK_REPLY
from manager_agent import process_manager_message
from agent_config.seed_skills import SEED_SKILLS
from whatsapp_identity import normalize_whatsapp_id

_AGENT_CONFIG_DIR = Path(__file__).parent / "agent_config"
_FOLLOWUP_DISPATCH_LOCK_KEY = 18420815

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
    log.info("leadqualbot_started", model=agent.model, paused_users=len(paused_eids))
    try:
        yield
    finally:
        followup_task.cancel()
        with suppress(asyncio.CancelledError):
            await followup_task
        await Database.disconnect()


app = FastAPI(title="LeadQualBot", version="1.0.0", lifespan=lifespan)


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
    while True:
        await asyncio.sleep(settings.followup_dispatch_interval_seconds)
        await _dispatch_due_followups_once()


async def _acquire_followup_dispatch_lock():
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


async def _release_followup_dispatch_lock(conn) -> None:
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", _FOLLOWUP_DISPATCH_LOCK_KEY)
    finally:
        await Database.pool.release(conn)


async def _dispatch_due_followups_once() -> None:
    """Send all due follow-ups once. Split out for testing and auditability."""
    lock_conn = await _acquire_followup_dispatch_lock()
    if lock_conn is None:
        log.debug("followup_dispatcher_lock_skipped")
        return
    try:
        due = await Database.get_due_followups()
        for fu in due:
            try:
                if not fu.get("message_id"):
                    stored = await _store_outbound_message(
                        external_id=fu["external_id"],
                        channel=fu["channel"],
                        text=fu["message"],
                        conversation_id=fu.get("conversation_id"),
                        source="follow_up",
                    )
                    await Database.attach_followup_message(fu["id"], stored["id"])
                channel = fu["channel"]
                if channel == "whatsapp":
                    await _wa_send_text(fu["external_id"], fu["message"])
                elif channel == "facebook":
                    await _send_fb_reply(fu["external_id"], fu["message"])
                await Database.mark_followup_sent(fu["id"])
                await Database.record_tool_outcome(
                    tool_name="follow_up_dispatcher",
                    success=True,
                    reason=fu["follow_up_type"],
                    details={"follow_up_id": str(fu["id"]), "channel": channel},
                    user_id=fu["user_id"],
                    conversation_id=fu.get("conversation_id"),
                )
                log.info("followup_sent", type=fu["follow_up_type"], user=fu["external_id"])
            except Exception as exc:
                await Database.record_tool_outcome(
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
        await _release_followup_dispatch_lock(lock_conn)


# ------------------------------------------------------------------ #
# Facebook Messenger Webhook
# ------------------------------------------------------------------ #

@app.get("/webhook/messenger")
async def fb_verify(request: Request):
    """Facebook webhook verification challenge."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.fb_verify_token:
        log.info("fb_webhook_verified")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


def _is_valid_fb_signature(body: bytes, signature: Optional[str]) -> bool:
    if not settings.fb_app_secret or not signature:
        return False
    try:
        scheme, expected = signature.split("=", 1)
    except ValueError:
        return False
    if scheme != "sha256":
        return False
    digest = hmac.new(
        settings.fb_app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, expected)


@app.post("/webhook/messenger")
async def fb_webhook(request: Request):
    """Receive and process Facebook Messenger events."""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not _is_valid_fb_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Invalid Facebook signature")
    body = json.loads(raw_body)

    if body.get("object") != "page":
        return JSONResponse({"status": "ignored"})

    for entry in body.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            if not sender_id:
                continue

            # Only process plain text messages
            msg = event.get("message", {})
            if "text" not in msg:
                continue

            text = msg["text"]
            # Process asynchronously to return 200 immediately
            asyncio.create_task(_handle_fb_message(sender_id, text))

    return JSONResponse({"status": "ok"})


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
_wa_debounce: dict[str, asyncio.Task] = {}
_wa_buffer: dict[str, list[str]] = {}
_wa_name: dict[str, Optional[str]] = {}
WA_DEBOUNCE_SECONDS = 15  # wait this long after last message before replying

# Human takeover pause — bare numbers (without @c.us/@lid) currently paused
# Populated from DB on startup and kept in sync by pause/unpause actions
_wa_paused_set: set[str] = set()

# Prospect test mode — when True, manager messages are routed through the sales agent
_wa_prospect_mode: bool = False
_PROSPECT_TEST_ID = "prospect_test_internal"

ADMIN_WA_NUMBERS: list[str] = settings.admin_wa_numbers
DASHBOARD_URL: str = settings.dashboard_url


def _is_paused(sender_id: str) -> bool:
    normalized = normalize_whatsapp_id(sender_id)
    return bool(normalized and normalized in _wa_paused_set)


def _set_paused_memory(external_id: str, paused: bool) -> None:
    bare = normalize_whatsapp_id(external_id)
    if not bare:
        return
    if paused:
        _wa_paused_set.add(bare)
    else:
        _wa_paused_set.discard(bare)


def _pause_action_secret() -> str:
    return settings.pause_action_secret or settings.wa_verify_token


def _extract_admin_token(
    authorization: Optional[str],
    x_admin_token: Optional[str],
) -> Optional[str]:
    if x_admin_token:
        return x_admin_token.strip() or None
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _require_admin_access(
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None),
) -> None:
    expected = settings.admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API token is not configured")
    provided = _extract_admin_token(authorization, x_admin_token)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _build_pause_action_token(conv_id: UUID, paused: bool) -> str:
    payload = f"{conv_id}:{int(paused)}".encode()
    return hmac.new(_pause_action_secret().encode(), payload, hashlib.sha256).hexdigest()


def _verify_pause_action_token(conv_id: UUID, paused: bool, token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, _build_pause_action_token(conv_id, paused))


def _wa_enqueue(sender_id: str, text: str, name: Optional[str]) -> None:
    """Buffer a message and (re)start the debounce timer for this sender."""
    # Manager messages bypass debounce — fire immediately
    if _is_manager(sender_id):
        asyncio.create_task(_wa_handle_message(sender_id, text, name))
        return

    # Paused conversations bypass debounce — shadow-log each message individually
    if _is_paused(sender_id):
        asyncio.create_task(_wa_shadow_log_customer(sender_id, text, name))
        return

    _wa_buffer.setdefault(sender_id, []).append(text)
    if name:
        _wa_name[sender_id] = name

    existing = _wa_debounce.get(sender_id)
    if existing and not existing.done():
        existing.cancel()

    async def _fire(sid: str):
        await asyncio.sleep(WA_DEBOUNCE_SECONDS)
        texts = _wa_buffer.pop(sid, [])
        display_name = _wa_name.pop(sid, None)
        _wa_debounce.pop(sid, None)
        if texts:
            await _wa_handle_message(sid, " ".join(texts), display_name)

    _wa_debounce[sender_id] = asyncio.create_task(_fire(sender_id))


@app.post("/webhook/whatsapp")
async def wa_webhook(request: Request):
    """Receive inbound WhatsApp events from Evolution API."""
    body = await request.json()

    event = body.get("event")

    # Only care about new inbound messages
    if event != "messages.upsert":
        return JSONResponse({"status": "ignored"})

    data = body.get("data", {})

    # Skip messages sent by us
    if data.get("key", {}).get("fromMe"):
        return JSONResponse({"status": "ignored"})

    sender_jid = data.get("key", {}).get("remoteJid", "")   # "60123456789@s.whatsapp.net"
    sender_id = normalize_whatsapp_id(sender_jid)
    name       = data.get("pushName")

    # Skip group messages
    if "@g.us" in sender_jid:
        return JSONResponse({"status": "group_ignored"})

    # Extract text from different message types
    msg  = data.get("message", {})
    text = (
        msg.get("conversation")
        or msg.get("extendedTextMessage", {}).get("text")
        or msg.get("buttonsResponseMessage", {}).get("selectedDisplayText")
        or msg.get("listResponseMessage", {}).get("title")
    )

    if not sender_id:
        return JSONResponse({"status": "ignored"})

    if not text:
        # Non-text message (image, audio, sticker…)
        asyncio.create_task(_wa_handle_unsupported(sender_id))
        return JSONResponse({"status": "ok"})

    _wa_enqueue(sender_id, text, name)
    return JSONResponse({"status": "ok"})


async def _wa_shadow_log_customer(sender_id: str, text: str, name: Optional[str]) -> None:
    """Store a customer message received while Maya is paused — no agent processing."""
    try:
        user = await Database.get_or_create_user(sender_id, "whatsapp")
        if name and not user.get("display_name"):
            await Database.update_user_display_name(user["id"], name)
        conv = await Database.get_active_conversation(user["id"])
        if not conv:
            conv = await Database.create_conversation(user["id"])
        await Database.store_message(conv["id"], "user", text, source="customer_paused")
        log.info("shadow_log_customer", sender_id=sender_id, preview=text[:60])
    except Exception as exc:
        log.error("shadow_log_customer_error", sender_id=sender_id, error=str(exc))


async def _store_outbound_message(
    external_id: str,
    channel: str,
    text: str,
    conversation_id: Optional[UUID] = None,
    source: str = "maya",
) -> Optional[dict]:
    """Persist an outbound customer-facing message before transport send."""
    user = await Database.get_or_create_user(external_id, channel)
    conv = {"id": conversation_id} if conversation_id else await Database.get_active_conversation(user["id"])
    if not conv:
        conv = await Database.create_conversation(user["id"])
    return await Database.store_message(conv["id"], "assistant", text, source=source)


def _format_catchup(shadow_messages: list) -> str:
    """Format shadow messages into a catch-up context block injected before Maya resumes."""
    lines = ["[ADMIN TAKEOVER — o below is what happened while Maya was paused. Resume naturally.]"]
    for m in shadow_messages:
        source = m.get("source", "")
        label = "Admin" if source == "admin" else "Customer"
        lines.append(f"{label}: {m['content']}")
    lines.append("[Maya resuming now]")
    return "\n".join(lines)


class AdminReplyMessage(BaseModel):
    recipient_id: str
    text: str


@app.post("/internal/wa/admin-reply")
async def wa_admin_reply(msg: AdminReplyMessage, _: None = Depends(_require_admin_access)):
    """Called by the WA gateway when the admin manually replies to a customer."""
    asyncio.create_task(_handle_admin_reply(msg.recipient_id, msg.text))
    return {"status": "ok"}


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


class PauseRequest(BaseModel):
    paused: bool
    token: str


@app.post("/api/wa/pause/{conv_id}")
async def set_pause_by_conv(conv_id: UUID, body: PauseRequest, _: None = Depends(_require_admin_access)):
    """Dashboard toggle — called by conversation detail page JS."""
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


def _is_manager(sender_id: str) -> bool:
    """Check if the sender is the configured manager/owner."""
    mgr = settings.manager_wa_id
    if not mgr:
        return False
    return normalize_whatsapp_id(sender_id) == normalize_whatsapp_id(mgr)


_MANAGER_COMMANDS = {
    "prospect on":    "prospect_on",
    "/prospect on":   "prospect_on",
    "prospect off":   "prospect_off",
    "prospect stop":  "prospect_off",
    "/prospect off":  "prospect_off",
    "/prospect stop": "prospect_off",
    "prospect reset": "prospect_reset",
    "/prospect reset":"prospect_reset",
    "reload":         "reload",
    "/reload":        "reload",
    "reload prompt":  "reload",
    "show prompt":    "show_prompt",
    "/show prompt":   "show_prompt",
    "show rules":     "show_rules",
    "/show rules":    "show_rules",
}


def _detect_manager_command(text: str) -> Optional[str]:
    t = text.strip().lower()
    if t in _MANAGER_COMMANDS:
        return _MANAGER_COMMANDS[t]
    if t.startswith("add rule:") or t.startswith("/add rule:"):
        return "add_rule"
    return None


async def _execute_manager_command(sender_id: str, command: str, raw_text: str) -> None:
    global _wa_prospect_mode

    if command == "prospect_on":
        _wa_prospect_mode = True
        await _wa_send_text(
            sender_id,
            "Prospect mode ON. Your messages will now go to Maya as a fresh lead.\n\n"
            "Commands while in prospect mode:\n"
            "- 'prospect off' — exit and return to manager mode\n"
            "- 'prospect reset' — clear test conversation history",
        )

    elif command == "prospect_off":
        _wa_prospect_mode = False
        await _wa_send_text(sender_id, "Prospect mode OFF. Back to manager mode.")

    elif command == "prospect_reset":
        try:
            user = await Database.get_or_create_user(_PROSPECT_TEST_ID, "whatsapp")
            conv = await Database.get_active_conversation(user["id"])
            if conv:
                await Database.delete_conversation(conv["id"])
                await _wa_send_text(sender_id, "Prospect test conversation cleared. Fresh start on next 'prospect on'.")
            else:
                await _wa_send_text(sender_id, "No active prospect test conversation to clear.")
        except Exception as exc:
            await _wa_send_text(sender_id, f"Reset failed: {str(exc)[:150]}")

    elif command == "reload":
        try:
            reload_prompt()
            await _wa_send_text(sender_id, "System prompt reloaded from file.")
        except Exception as exc:
            await _wa_send_text(sender_id, f"Reload failed: {str(exc)[:150]}")

    elif command == "show_prompt":
        try:
            content = (_AGENT_CONFIG_DIR / "system_prompt.md").read_text()
            await _wa_send_text(sender_id, content)
        except Exception as exc:
            await _wa_send_text(sender_id, f"Error reading prompt: {str(exc)}")

    elif command == "show_rules":
        try:
            content = (_AGENT_CONFIG_DIR / "system_prompt.md").read_text()
            start = content.find("## NEVER")
            if start == -1:
                await _wa_send_text(sender_id, "NEVER section not found.")
                return
            end = content.find("\n---\n", start)
            section = content[start:end].strip() if end != -1 else content[start:].strip()
            await _wa_send_text(sender_id, section)
        except Exception as exc:
            await _wa_send_text(sender_id, f"Error: {str(exc)}")

    elif command == "add_rule":
        colon_idx = raw_text.lower().find("add rule:")
        rule_text = raw_text[colon_idx + len("add rule:"):].strip()
        if not rule_text:
            await _wa_send_text(sender_id, "Usage: add rule: <your rule here>")
            return
        try:
            path = _AGENT_CONFIG_DIR / "system_prompt.md"
            content = path.read_text()
            never_idx = content.find("## NEVER\n")
            if never_idx == -1:
                await _wa_send_text(sender_id, "NEVER section not found in prompt.")
                return
            end_marker = content.find("\n---\n", never_idx)
            insert_pos = end_marker if end_marker != -1 else len(content)
            new_content = content[:insert_pos] + f"- {rule_text}\n" + content[insert_pos:]
            path.write_text(new_content)
            reload_prompt()
            await _wa_send_text(sender_id, f"Rule added and prompt reloaded:\n- {rule_text}")
        except Exception as exc:
            await _wa_send_text(sender_id, f"Error adding rule: {str(exc)[:150]}")


async def _wa_handle_as_prospect(manager_id: str, text: str, name: Optional[str]) -> None:
    """Route manager through the normal sales agent using a clean test external_id."""
    try:
        reply = await agent.process_message(
            external_id=_PROSPECT_TEST_ID,
            message=text,
            channel="whatsapp",
        )
        parts = [p.strip() for p in reply.split("\n---\n") if p.strip()]
        for i, part in enumerate(parts):
            await _wa_send_text(manager_id, part)
            if i < len(parts) - 1:
                await asyncio.sleep(1.5)
    except AgentError as exc:
        await _wa_send_text(manager_id, f"[Maya error in prospect test: {str(exc)[:150]}]")
    except Exception as exc:
        log.error("prospect_test_error", error=str(exc))
        await _wa_send_text(manager_id, f"[Error: {str(exc)[:150]}]")


async def _wa_handle_manager(sender_id: str, text: str):
    """Handle a message from the business owner — reporting mode."""
    log.info("manager_message_received", sender_id=sender_id, preview=text[:80])
    try:
        reply = await process_manager_message(text)
        await _wa_send_text(sender_id, reply)
    except Exception as exc:
        log.error("manager_handler_error", error=str(exc))
        try:
            await _wa_send_text(sender_id, f"Error: {str(exc)[:200]}")
        except Exception:
            pass


async def _check_trial_gate(sender_id: str) -> Optional[str]:
    """
    If user is on a trial, enforce daily message limit and 7-day expiry.
    Returns a block message to send, or None if the user can proceed.
    """
    user = await Database.get_or_create_user(sender_id, "whatsapp")
    facts = await Database.get_user_facts(user["id"])

    if not facts.get("trial_active"):
        return None

    # Check 7-day expiry
    trial_start = facts.get("trial_start")
    if trial_start:
        try:
            start_dt = datetime.fromisoformat(trial_start).replace(tzinfo=timezone.utc)
            days_elapsed = (datetime.now(timezone.utc) - start_dt).days
            if days_elapsed >= 7:
                log.info("trial_expired", sender_id=sender_id, days=days_elapsed)
                return (
                    "Hey! Trial 7 hari you dah tamat. "
                    "Nak sambung guna AI agent ni? Set up meeting dengan team kita — "
                    "taip \"nak proceed\" dan kita akan reach out."
                )
        except (ValueError, TypeError):
            pass

    # Check daily message limit
    count = await Database.count_user_messages_today(user["id"])
    limit = settings.trial_daily_message_limit
    if count >= limit:
        log.info("trial_limit_reached", sender_id=sender_id, count=count, limit=limit)
        return (
            f"You dah guna {limit} messages untuk hari ni — tu limit trial you. "
            "Cuba lagi esok, atau taip \"nak proceed\" kalau nak full setup."
        )

    return None


async def _notify_handoff_admins(external_id: str, user_id: UUID, conv_id: Optional[UUID], reason: str, priority: str, notes: Optional[str], display: str) -> None:
    """Send WA message to each admin number when Maya hands off a lead."""
    priority_label = {"high": "🔴 HIGH", "medium": "🟡 MEDIUM", "low": "🟢 LOW"}.get(priority, priority.upper())
    reason_str = reason.replace("_", " ").title()
    conv_url = f"{DASHBOARD_URL}/conversations/{conv_id}" if conv_id else DASHBOARD_URL
    lines = [
        f"🔔 Lead Handoff — Maya needs you",
        f"Customer: {display}",
        f"Priority: {priority_label}",
        f"Reason: {reason_str}",
    ]
    if notes:
        lines.append(f"Notes: {notes[:150]}")
    lines += ["", f"Manage in dashboard: {conv_url}"]
    msg = "\n".join(lines)
    for admin_num in ADMIN_WA_NUMBERS:
        try:
            await _wa_send_text(admin_num, msg)
        except Exception as exc:
            log.warning("admin_wa_notify_failed", admin=admin_num, error=str(exc))


async def _wa_handle_message(sender_id: str, text: str, name: Optional[str]):
    """Process an inbound WhatsApp message and send Maya's reply."""
    # Manager bypass — commands always checked first regardless of mode
    if _is_manager(sender_id):
        cmd = _detect_manager_command(text)
        if cmd:
            await _execute_manager_command(sender_id, cmd, text)
            return
        if _wa_prospect_mode:
            await _wa_handle_as_prospect(sender_id, text, name)
        else:
            await _wa_handle_manager(sender_id, text)
        return

    display = name or sender_id

    # Edge case: admin may have replied during the debounce window — check again
    if _is_paused(sender_id):
        user = await Database.get_or_create_user(sender_id, "whatsapp")
        conv = await Database.get_active_conversation(user["id"])
        if conv:
            await Database.store_message(conv["id"], "user", text, source="customer_paused")
        log.info("message_dropped_paused_during_debounce", sender_id=sender_id)
        return

    try:
        # Trial gate — check before touching the agent
        block_msg = await _check_trial_gate(sender_id)
        if block_msg:
            await _store_outbound_message(sender_id, "whatsapp", block_msg)
            await _wa_send_text(sender_id, block_msg)
            return
        # Store display name on first contact
        if name:
            user = await Database.get_or_create_user(sender_id, "whatsapp")
            if not user.get("display_name"):
                await Database.update_user_display_name(user["id"], name)

        user = await Database.get_or_create_user(sender_id, "whatsapp")

        # Catch-up injection: if we just resumed from a pause, inject shadow log as context
        pause = await Database.get_pause_state(user["id"])
        if pause["paused_at"] and not pause["paused"]:
            conv = await Database.get_active_conversation(user["id"])
            if conv:
                shadow = await Database.get_shadow_messages(conv["id"], pause["paused_at"])
                if shadow:
                    catchup = _format_catchup(shadow)
                    await Database.store_message(conv["id"], "user", catchup, source="catchup")
                    log.info("catchup_injected", sender_id=sender_id, shadow_count=len(shadow))
            await Database.clear_pause_history(user["id"])

        # Alert owner only on first message of a new conversation
        conv = await Database.get_active_conversation(user["id"])
        is_new = conv is None or (await Database.get_conversation_history(conv["id"], limit=1)) == []
        if is_new:
            asyncio.create_task(_tg_alert(
                f"💬 <b>New conversation</b> — {display}\n{text[:200]}"
            ))

        reply = await agent.process_message(
            external_id=sender_id,
            message=text,
            channel="whatsapp",
        )
        parts = [p.strip() for p in reply.split("\n---\n") if p.strip()]
        for i, part in enumerate(parts):
            await _wa_send_text(sender_id, part)
            if i < len(parts) - 1:
                await asyncio.sleep(1.5)

    except Exception as exc:
        log.error("wa_message_handling_error", error=str(exc))
        asyncio.create_task(_tg_alert(
            f"⚠️ <b>Maya error</b> — {display}\nMessage: {text[:200]}\n<code>{str(exc)[:300]}</code>"
        ))
        try:
            await _store_outbound_message(sender_id, "whatsapp", _FALLBACK_REPLY)
            await _wa_send_text(sender_id, _FALLBACK_REPLY)
        except Exception as send_exc:
            log.error("wa_fallback_send_failed", sender_id=sender_id, error=str(send_exc))



async def _wa_handle_unsupported(sender_id: str):
    """Reply to non-text messages (voice, image, sticker, etc.)."""
    text = "Hai! Saya Maya 😊 Saya hanya boleh baca teks buat masa ni. Boleh taip soalan awak?"
    await _store_outbound_message(sender_id, "whatsapp", text)
    await _wa_send_text(
        sender_id,
        text
    )


_SEND_RETRIES = 3
_SEND_BACKOFF_BASE = 2.0  # seconds


async def _wa_send_text(to: str, text: str):
    """Send a text message via the local WA Gateway (whatsapp-web.js)."""
    if not text or not text.strip():
        log.error("wa_send_text_empty_guard_triggered", to=to)
        raise ValueError(f"_wa_send_text: empty text for to={to}")
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            for attempt in range(_SEND_RETRIES):
                try:
                    resp = await client.post(
                        f"{settings.wa_gateway_base_url.rstrip('/')}/send",
                        json={"number": to, "text": chunk},
                    )
                    if resp.status_code == 200:
                        break
                    raise RuntimeError(f"status={resp.status_code} body={resp.text[:200]}")
                except Exception as exc:
                    if attempt == _SEND_RETRIES - 1:
                        log.error("wa_send_failed", to=to, attempt=attempt + 1, error=str(exc))
                        raise
                    wait = _SEND_BACKOFF_BASE ** attempt
                    log.warning("wa_send_retry", to=to, attempt=attempt + 1,
                                wait=wait, error=str(exc))
                    await asyncio.sleep(wait)
    log.info("wa_send_ok", to=to, reply_len=len(text), chunks=len(chunks))


# ------------------------------------------------------------------ #
# Test / Simulator Chat API
# ------------------------------------------------------------------ #

class ChatRequest(BaseModel):
    user_id: str
    message: str
    channel: str = "test"


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Simulate a conversation without connecting to a real channel."""
    response_text = await agent.process_message(
        external_id=req.user_id,
        message=req.message,
        channel=req.channel,
    )
    return {"reply": response_text}


class WAInternalMessage(BaseModel):
    sender_id: str
    text: str
    name: Optional[str] = None


@app.post("/internal/wa")
async def wa_internal(msg: WAInternalMessage, _: None = Depends(_require_admin_access)):
    """Receive inbound WhatsApp messages from the local WA Gateway."""
    _wa_enqueue(msg.sender_id, msg.text, msg.name)
    return {"status": "ok"}


@app.get("/api/chat/{external_id}/history")
async def chat_history(external_id: str):
    user = await Database.get_user_by_external_id(external_id)
    if not user:
        return {"messages": []}
    conv = await Database.get_active_conversation(user["id"])
    if not conv:
        return {"messages": []}
    history = await Database.get_conversation_history(conv["id"], limit=100)
    return {"messages": history}


# ------------------------------------------------------------------ #
# Dashboard Routes


@app.get("/dashboard/analytics", response_class=HTMLResponse)
async def dashboard_analytics(request: Request):
    analytics_data = await Database.get_analytics()  # Fetch analytics data
    return templates.TemplateResponse(
        "analytics.html",
        {"request": request, "analytics": analytics_data, "page": "analytics"},
    )

# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    analytics = await Database.get_analytics()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "analytics": analytics, "page": "overview"},
    )


@app.get("/conversations", response_class=HTMLResponse)
async def dashboard_conversations(
    request: Request,
    page: int = Query(1, ge=1),
):
    limit = 20
    offset = (page - 1) * limit
    conversations = await Database.list_conversations(limit=limit, offset=offset)
    return templates.TemplateResponse(
        "conversations.html",
        {
            "request": request,
            "conversations": conversations,
            "page": page,
            "has_next": len(conversations) == limit,
            "active_page": "conversations",
        },
    )


@app.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(request: Request, conv_id: UUID):
    detail = await Database.get_conversation_detail(conv_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Conversation not found")
    audit = await Database.get_facts_audit(detail["user_id"])
    pause = await Database.get_pause_state(detail["user_id"])
    return templates.TemplateResponse(
        "conversation_detail.html",
        {
            "request": request,
            "conv": detail,
            "audit": audit,
            "maya_paused": pause["paused"],
            "pause_token": _build_pause_action_token(conv_id, True),
            "resume_token": _build_pause_action_token(conv_id, False),
            "active_page": "conversations",
        },
    )


@app.get("/leads", response_class=HTMLResponse)
async def dashboard_leads(
    request: Request,
    page: int = Query(1, ge=1),
):
    limit = 20
    offset = (page - 1) * limit
    leads = await Database.list_leads(limit=limit, offset=offset)
    return templates.TemplateResponse(
        "leads.html",
        {
            "request": request,
            "leads": leads,
            "page": page,
            "has_next": len(leads) == limit,
            "active_page": "leads",
        },
    )


@app.get("/handoffs", response_class=HTMLResponse)
async def dashboard_handoffs(
    request: Request,
    status: str = Query("pending"),
):
    handoffs = await Database.list_handoffs(status=status)
    return templates.TemplateResponse(
        "handoffs.html",
        {
            "request": request,
            "handoffs": handoffs,
            "filter_status": status,
            "active_page": "handoffs",
        },
    )


# ------------------------------------------------------------------ #
# Dashboard API Actions
# ------------------------------------------------------------------ #

@app.patch("/api/handoffs/{handoff_id}")
async def update_handoff(handoff_id: UUID, body: dict, _: None = Depends(_require_admin_access)):
    status = body.get("status")
    if status not in ("in_progress", "resolved"):
        raise HTTPException(status_code=400, detail="Invalid status")
    assigned_to = body.get("assigned_to")
    await Database.update_handoff_status(handoff_id, status, assigned_to)
    return {"ok": True}


@app.patch("/api/leads/{user_id}/facts")
async def update_lead_facts(user_id: UUID, body: dict, _: None = Depends(_require_admin_access)):
    """Admin endpoint to directly edit a lead's structured facts."""
    facts = body.get("facts", {})
    if not facts:
        raise HTTPException(status_code=400, detail="No facts provided")
    updated = await Database.update_facts(user_id, facts, changed_by="admin")
    return {"ok": True, "facts": updated.get("facts", {})}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: UUID, _: None = Depends(_require_admin_access)):
    """Delete a conversation and all its messages."""
    await Database.delete_conversation(conv_id)
    return {"ok": True}


@app.get("/api/analytics")
async def api_analytics():
    return await Database.get_analytics()


@app.get("/api/learning")
async def api_learning():
    return await Database.get_learning_stats()


@app.get("/learning", response_class=HTMLResponse)
async def dashboard_learning(request: Request):
    stats = await Database.get_learning_stats()
    return templates.TemplateResponse(
        "learning.html",
        {"request": request, "stats": stats, "active_page": "learning"},
    )


@app.get("/wa-qr", response_class=HTMLResponse)
async def wa_qr():
    """Proxy to the WA Gateway QR code page."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{settings.wa_gateway_base_url.rstrip('/')}/qr")
            return HTMLResponse(content=resp.text, status_code=resp.status_code)
        except Exception:
            return HTMLResponse(content="<h2>WA Gateway not running</h2>", status_code=503)
