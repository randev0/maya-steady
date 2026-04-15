import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import UUID

import httpx

from whatsapp_identity import normalize_whatsapp_id


@dataclass
class WhatsAppRuntimeState:
    debounce_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    buffered_texts: dict[str, list[str]] = field(default_factory=dict)
    display_names: dict[str, Optional[str]] = field(default_factory=dict)
    paused_numbers: set[str] = field(default_factory=set)
    prospect_mode: bool = False


def is_paused(state: WhatsAppRuntimeState, sender_id: str) -> bool:
    normalized = normalize_whatsapp_id(sender_id)
    return bool(normalized and normalized in state.paused_numbers)


def set_paused_memory(state: WhatsAppRuntimeState, external_id: str, paused: bool) -> None:
    bare = normalize_whatsapp_id(external_id)
    if not bare:
        return
    if paused:
        state.paused_numbers.add(bare)
    else:
        state.paused_numbers.discard(bare)


def is_manager(settings, sender_id: str) -> bool:
    mgr = settings.manager_wa_id
    if not mgr:
        return False
    return normalize_whatsapp_id(sender_id) == normalize_whatsapp_id(mgr)


_MANAGER_COMMANDS = {
    "prospect on": "prospect_on",
    "/prospect on": "prospect_on",
    "prospect off": "prospect_off",
    "prospect stop": "prospect_off",
    "/prospect off": "prospect_off",
    "/prospect stop": "prospect_off",
    "prospect reset": "prospect_reset",
    "/prospect reset": "prospect_reset",
    "reload": "reload",
    "/reload": "reload",
    "reload prompt": "reload",
    "show prompt": "show_prompt",
    "/show prompt": "show_prompt",
    "show rules": "show_rules",
    "/show rules": "show_rules",
}


def detect_manager_command(text: str) -> Optional[str]:
    t = text.strip().lower()
    if t in _MANAGER_COMMANDS:
        return _MANAGER_COMMANDS[t]
    if t.startswith("add rule:") or t.startswith("/add rule:"):
        return "add_rule"
    return None


async def enqueue_message(
    *,
    state: WhatsAppRuntimeState,
    sender_id: str,
    text: str,
    name: Optional[str],
    debounce_seconds: int,
    is_manager_fn: Callable[[str], bool],
    is_paused_fn: Callable[[str], bool],
    handle_message: Callable[[str, str, Optional[str]], asyncio.Future],
    shadow_log_customer: Callable[[str, str, Optional[str]], asyncio.Future],
) -> None:
    if is_manager_fn(sender_id):
        asyncio.create_task(handle_message(sender_id, text, name))
        return

    if is_paused_fn(sender_id):
        asyncio.create_task(shadow_log_customer(sender_id, text, name))
        return

    state.buffered_texts.setdefault(sender_id, []).append(text)
    if name:
        state.display_names[sender_id] = name

    existing = state.debounce_tasks.get(sender_id)
    if existing and not existing.done():
        existing.cancel()

    async def fire(sid: str):
        await asyncio.sleep(debounce_seconds)
        texts = state.buffered_texts.pop(sid, [])
        display_name = state.display_names.pop(sid, None)
        state.debounce_tasks.pop(sid, None)
        if texts:
            await handle_message(sid, " ".join(texts), display_name)

    state.debounce_tasks[sender_id] = asyncio.create_task(fire(sender_id))


async def shadow_log_customer(db, log, sender_id: str, text: str, name: Optional[str]) -> None:
    try:
        user = await db.get_or_create_user(sender_id, "whatsapp")
        if name and not user.get("display_name"):
            await db.update_user_display_name(user["id"], name)
        conv = await db.get_active_conversation(user["id"])
        if not conv:
            conv = await db.create_conversation(user["id"])
        await db.store_message(conv["id"], "user", text, source="customer_paused")
        log.info("shadow_log_customer", sender_id=sender_id, preview=text[:60])
    except Exception as exc:
        log.error("shadow_log_customer_error", sender_id=sender_id, error=str(exc))


async def store_outbound_message(db, external_id: str, channel: str, text: str, conversation_id=None, source: str = "maya"):
    user = await db.get_or_create_user(external_id, channel)
    conv = {"id": conversation_id} if conversation_id else await db.get_active_conversation(user["id"])
    if not conv:
        conv = await db.create_conversation(user["id"])
    return await db.store_message(conv["id"], "assistant", text, source=source)


def format_catchup(shadow_messages: list) -> str:
    lines = ["[ADMIN TAKEOVER — o below is what happened while Maya was paused. Resume naturally.]"]
    for m in shadow_messages:
        source = m.get("source", "")
        label = "Admin" if source == "admin" else "Customer"
        lines.append(f"{label}: {m['content']}")
    lines.append("[Maya resuming now]")
    return "\n".join(lines)


async def execute_manager_command(
    *,
    state: WhatsAppRuntimeState,
    db,
    agent,
    send_text,
    reload_prompt,
    prompt_dir: Path,
    sender_id: str,
    command: str,
    raw_text: str,
    log,
    fallback_reply: str,
    prospect_test_id: str,
) -> None:
    if command == "prospect_on":
        state.prospect_mode = True
        await send_text(
            sender_id,
            "Prospect mode ON. Your messages will now go to Maya as a fresh lead.\n\n"
            "Commands while in prospect mode:\n"
            "- 'prospect off' — exit and return to manager mode\n"
            "- 'prospect reset' — clear test conversation history",
        )
    elif command == "prospect_off":
        state.prospect_mode = False
        await send_text(sender_id, "Prospect mode OFF. Back to manager mode.")
    elif command == "prospect_reset":
        try:
            user = await db.get_or_create_user(prospect_test_id, "whatsapp")
            conv = await db.get_active_conversation(user["id"])
            if conv:
                await db.delete_conversation(conv["id"])
                await send_text(sender_id, "Prospect test conversation cleared. Fresh start on next 'prospect on'.")
            else:
                await send_text(sender_id, "No active prospect test conversation to clear.")
        except Exception as exc:
            await send_text(sender_id, f"Reset failed: {str(exc)[:150]}")
    elif command == "reload":
        try:
            reload_prompt()
            await send_text(sender_id, "System prompt reloaded from file.")
        except Exception as exc:
            await send_text(sender_id, f"Reload failed: {str(exc)[:150]}")
    elif command == "show_prompt":
        try:
            content = (prompt_dir / "system_prompt.md").read_text()
            await send_text(sender_id, content)
        except Exception as exc:
            await send_text(sender_id, f"Error reading prompt: {str(exc)}")
    elif command == "show_rules":
        try:
            content = (prompt_dir / "system_prompt.md").read_text()
            start = content.find("## NEVER")
            if start == -1:
                await send_text(sender_id, "NEVER section not found.")
                return
            end = content.find("\n---\n", start)
            section = content[start:end].strip() if end != -1 else content[start:].strip()
            await send_text(sender_id, section)
        except Exception as exc:
            await send_text(sender_id, f"Error: {str(exc)}")
    elif command == "add_rule":
        colon_idx = raw_text.lower().find("add rule:")
        rule_text = raw_text[colon_idx + len("add rule:"):].strip()
        if not rule_text:
            await send_text(sender_id, "Usage: add rule: <your rule here>")
            return
        try:
            path = prompt_dir / "system_prompt.md"
            content = path.read_text()
            never_idx = content.find("## NEVER\n")
            if never_idx == -1:
                await send_text(sender_id, "NEVER section not found in prompt.")
                return
            end_marker = content.find("\n---\n", never_idx)
            insert_pos = end_marker if end_marker != -1 else len(content)
            new_content = content[:insert_pos] + f"- {rule_text}\n" + content[insert_pos:]
            path.write_text(new_content)
            reload_prompt()
            await send_text(sender_id, f"Rule added and prompt reloaded:\n- {rule_text}")
        except Exception as exc:
            await send_text(sender_id, f"Error adding rule: {str(exc)[:150]}")


async def handle_as_prospect(*, agent, send_text, manager_id: str, text: str, prospect_test_id: str, log) -> None:
    try:
        reply = await agent.process_message(
            external_id=prospect_test_id,
            message=text,
            channel="whatsapp",
        )
        parts = [p.strip() for p in reply.split("\n---\n") if p.strip()]
        for i, part in enumerate(parts):
            await send_text(manager_id, part)
            if i < len(parts) - 1:
                await asyncio.sleep(1.5)
    except Exception as exc:
        log.error("prospect_test_error", error=str(exc))
        await send_text(manager_id, f"[Error: {str(exc)[:150]}]")


async def handle_manager(*, process_manager_message, send_text, sender_id: str, text: str, log) -> None:
    log.info("manager_message_received", sender_id=sender_id, preview=text[:80])
    try:
        reply = await process_manager_message(text)
        await send_text(sender_id, reply)
    except Exception as exc:
        log.error("manager_handler_error", error=str(exc))
        try:
            await send_text(sender_id, f"Error: {str(exc)[:200]}")
        except Exception:
            pass


async def check_trial_gate(*, db, settings, log, sender_id: str) -> Optional[str]:
    user = await db.get_or_create_user(sender_id, "whatsapp")
    facts = await db.get_user_facts(user["id"])
    if not facts.get("trial_active"):
        return None
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
    count = await db.count_user_messages_today(user["id"])
    limit = settings.trial_daily_message_limit
    if count >= limit:
        log.info("trial_limit_reached", sender_id=sender_id, count=count, limit=limit)
        return (
            f"You dah guna {limit} messages untuk hari ni — tu limit trial you. "
            "Cuba lagi esok, atau taip \"nak proceed\" kalau nak full setup."
        )
    return None


async def notify_handoff_admins(*, settings, send_text, conv_id: Optional[UUID], reason: str, priority: str, notes: Optional[str], display: str, log) -> None:
    priority_label = {"high": "🔴 HIGH", "medium": "🟡 MEDIUM", "low": "🟢 LOW"}.get(priority, priority.upper())
    reason_str = reason.replace("_", " ").title()
    conv_url = f"{settings.dashboard_url}/conversations/{conv_id}" if conv_id else settings.dashboard_url
    lines = [
        "🔔 Lead Handoff — Maya needs you",
        f"Customer: {display}",
        f"Priority: {priority_label}",
        f"Reason: {reason_str}",
    ]
    if notes:
        lines.append(f"Notes: {notes[:150]}")
    lines += ["", f"Manage in dashboard: {conv_url}"]
    msg = "\n".join(lines)
    for admin_num in settings.admin_wa_numbers:
        try:
            await send_text(admin_num, msg)
        except Exception as exc:
            log.warning("admin_wa_notify_failed", admin=admin_num, error=str(exc))


async def handle_message(
    *,
    state: WhatsAppRuntimeState,
    db,
    settings,
    agent,
    send_text,
    store_outbound_message_fn,
    tg_alert,
    process_manager_message,
    reload_prompt,
    prompt_dir: Path,
    sender_id: str,
    text: str,
    name: Optional[str],
    is_manager_fn: Callable[[str], bool],
    is_paused_fn: Callable[[str], bool],
    set_paused_memory_fn: Callable[[str, bool], None],
    detect_manager_command_fn: Callable[[str], Optional[str]],
    log,
    fallback_reply: str,
    prospect_test_id: str,
) -> None:
    if is_manager_fn(sender_id):
        cmd = detect_manager_command_fn(text)
        if cmd:
            await execute_manager_command(
                state=state,
                db=db,
                agent=agent,
                send_text=send_text,
                reload_prompt=reload_prompt,
                prompt_dir=prompt_dir,
                sender_id=sender_id,
                command=cmd,
                raw_text=text,
                log=log,
                fallback_reply=fallback_reply,
                prospect_test_id=prospect_test_id,
            )
            return
        if state.prospect_mode:
            await handle_as_prospect(
                agent=agent,
                send_text=send_text,
                manager_id=sender_id,
                text=text,
                prospect_test_id=prospect_test_id,
                log=log,
            )
        else:
            await handle_manager(
                process_manager_message=process_manager_message,
                send_text=send_text,
                sender_id=sender_id,
                text=text,
                log=log,
            )
        return

    display = name or sender_id
    if is_paused_fn(sender_id):
        user = await db.get_or_create_user(sender_id, "whatsapp")
        conv = await db.get_active_conversation(user["id"])
        if conv:
            await db.store_message(conv["id"], "user", text, source="customer_paused")
        log.info("message_dropped_paused_during_debounce", sender_id=sender_id)
        return

    try:
        block_msg = await check_trial_gate(db=db, settings=settings, log=log, sender_id=sender_id)
        if block_msg:
            await store_outbound_message_fn(sender_id, "whatsapp", block_msg)
            await send_text(sender_id, block_msg)
            return
        if name:
            user = await db.get_or_create_user(sender_id, "whatsapp")
            if not user.get("display_name"):
                await db.update_user_display_name(user["id"], name)
        user = await db.get_or_create_user(sender_id, "whatsapp")
        pause = await db.get_pause_state(user["id"])
        if pause["paused_at"] and not pause["paused"]:
            conv = await db.get_active_conversation(user["id"])
            if conv:
                shadow = await db.get_shadow_messages(conv["id"], pause["paused_at"])
                if shadow:
                    catchup = format_catchup(shadow)
                    await db.store_message(conv["id"], "user", catchup, source="catchup")
                    log.info("catchup_injected", sender_id=sender_id, shadow_count=len(shadow))
            await db.clear_pause_history(user["id"])

        conv = await db.get_active_conversation(user["id"])
        is_new = conv is None or (await db.get_conversation_history(conv["id"], limit=1)) == []
        if is_new:
            asyncio.create_task(tg_alert(f"💬 <b>New conversation</b> — {display}\n{text[:200]}"))

        reply = await agent.process_message(
            external_id=sender_id,
            message=text,
            channel="whatsapp",
        )
        parts = [p.strip() for p in reply.split("\n---\n") if p.strip()]
        for i, part in enumerate(parts):
            await send_text(sender_id, part)
            if i < len(parts) - 1:
                await asyncio.sleep(1.5)
    except Exception as exc:
        log.error("wa_message_handling_error", error=str(exc))
        asyncio.create_task(
            tg_alert(f"⚠️ <b>Maya error</b> — {display}\nMessage: {text[:200]}\n<code>{str(exc)[:300]}</code>")
        )
        try:
            await store_outbound_message_fn(sender_id, "whatsapp", fallback_reply)
            await send_text(sender_id, fallback_reply)
        except Exception as send_exc:
            log.error("wa_fallback_send_failed", sender_id=sender_id, error=str(send_exc))


async def handle_unsupported(*, store_outbound_message_fn, send_text, sender_id: str) -> None:
    text = "Hai! Saya Maya 😊 Saya hanya boleh baca teks buat masa ni. Boleh taip soalan awak?"
    await store_outbound_message_fn(sender_id, "whatsapp", text)
    await send_text(sender_id, text)


async def send_text(*, settings, log, to: str, text: str, retries: int = 3, backoff_base: float = 2.0) -> None:
    raise NotImplementedError


async def send_text_via_gateway(
    *,
    http_client_factory,
    settings,
    log,
    to: str,
    text: str,
    retries: int = 3,
    backoff_base: float = 2.0,
) -> None:
    if not text or not text.strip():
        log.error("wa_send_text_empty_guard_triggered", to=to)
        raise ValueError(f"_wa_send_text: empty text for to={to}")
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    async with http_client_factory(timeout=15) as client:
        for chunk in chunks:
            for attempt in range(retries):
                try:
                    resp = await client.post(
                        f"{settings.wa_gateway_base_url.rstrip('/')}/send",
                        json={"number": to, "text": chunk},
                    )
                    if resp.status_code == 200:
                        break
                    raise RuntimeError(f"status={resp.status_code} body={resp.text[:200]}")
                except Exception as exc:
                    if attempt == retries - 1:
                        log.error("wa_send_failed", to=to, attempt=attempt + 1, error=str(exc))
                        raise
                    wait = backoff_base ** attempt
                    log.warning("wa_send_retry", to=to, attempt=attempt + 1, wait=wait, error=str(exc))
                    await asyncio.sleep(wait)
    log.info("wa_send_ok", to=to, reply_len=len(text), chunks=len(chunks))
