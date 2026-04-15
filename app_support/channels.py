import asyncio
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, create_model

from whatsapp_identity import normalize_whatsapp_id


def build_channels_router(
    *,
    settings,
    require_admin_access,
    verify_pause_action_token,
    is_valid_fb_signature,
    handle_fb_message,
    send_fb_reply,
    wa_enqueue,
    handle_unsupported,
    handle_admin_reply,
    set_paused_memory,
    store_outbound_message,
    process_test_chat,
    get_chat_history,
    db,
    fallback_reply: str,
):
    router = APIRouter()

    ChatRequest = create_model(
        "ChatRequest",
        user_id=(str, ...),
        message=(str, ...),
        channel=(str, "test"),
    )
    WAInternalMessage = create_model(
        "WAInternalMessage",
        sender_id=(str, ...),
        text=(str, ...),
        name=(Optional[str], None),
    )
    AdminReplyMessage = create_model(
        "AdminReplyMessage",
        recipient_id=(str, ...),
        text=(str, ...),
    )
    PauseRequest = create_model(
        "PauseRequest",
        paused=(bool, ...),
        token=(str, ...),
    )

    @router.get("/webhook/messenger")
    async def fb_verify(request: Request):
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")
        if mode == "subscribe" and token == settings.fb_verify_token:
            return PlainTextResponse(challenge)
        raise HTTPException(status_code=403, detail="Verification failed")

    @router.post("/webhook/messenger")
    async def fb_webhook(request: Request):
        raw_body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not is_valid_fb_signature(raw_body, signature):
            raise HTTPException(status_code=403, detail="Invalid Facebook signature")
        body = json.loads(raw_body)
        if body.get("object") != "page":
            return JSONResponse({"status": "ignored"})
        for entry in body.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                if not sender_id:
                    continue
                msg = event.get("message", {})
                if "text" not in msg:
                    continue
                asyncio.create_task(handle_fb_message(sender_id, msg["text"]))
        return JSONResponse({"status": "ok"})

    @router.post("/webhook/whatsapp")
    async def wa_webhook(request: Request):
        body = await request.json()
        event = body.get("event")
        if event != "messages.upsert":
            return JSONResponse({"status": "ignored"})
        data = body.get("data", {})
        if data.get("key", {}).get("fromMe"):
            return JSONResponse({"status": "ignored"})
        sender_jid = data.get("key", {}).get("remoteJid", "")
        sender_id = normalize_whatsapp_id(sender_jid)
        name = data.get("pushName")
        if "@g.us" in sender_jid:
            return JSONResponse({"status": "group_ignored"})
        msg = data.get("message", {})
        text = (
            msg.get("conversation")
            or msg.get("extendedTextMessage", {}).get("text")
            or msg.get("buttonsResponseMessage", {}).get("selectedDisplayText")
            or msg.get("listResponseMessage", {}).get("title")
        )
        if not sender_id:
            return JSONResponse({"status": "ignored"})
        if not text:
            asyncio.create_task(handle_unsupported(sender_id))
            return JSONResponse({"status": "ok"})
        wa_enqueue(sender_id, text, name)
        return JSONResponse({"status": "ok"})

    @router.post("/internal/wa/admin-reply")
    async def wa_admin_reply(msg: AdminReplyMessage, _: None = Depends(require_admin_access)):
        asyncio.create_task(handle_admin_reply(msg.recipient_id, msg.text))
        return {"status": "ok"}

    @router.post("/api/wa/pause/{conv_id}")
    async def set_pause_by_conv(conv_id: UUID, body: PauseRequest, _: None = Depends(require_admin_access)):
        if not verify_pause_action_token(conv_id, body.paused, body.token):
            raise HTTPException(status_code=403, detail="Invalid pause token")
        detail = await db.get_conversation_detail(conv_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Conversation not found")
        user_id = detail["user_id"]
        external_id = detail["external_id"]
        set_paused_memory(external_id, body.paused)
        await db.set_user_paused(user_id, body.paused)
        return {"ok": True, "paused": body.paused}

    @router.post("/api/chat")
    async def chat(req: ChatRequest):
        response_text = await process_test_chat(req.user_id, req.message, req.channel)
        return {"reply": response_text}

    @router.post("/internal/wa")
    async def wa_internal(msg: WAInternalMessage, _: None = Depends(require_admin_access)):
        wa_enqueue(msg.sender_id, msg.text, msg.name)
        return {"status": "ok"}

    @router.get("/api/chat/{external_id}/history")
    async def chat_history(external_id: str):
        return await get_chat_history(external_id)

    return router, {
        "ChatRequest": ChatRequest,
        "WAInternalMessage": WAInternalMessage,
        "AdminReplyMessage": AdminReplyMessage,
        "PauseRequest": PauseRequest,
        "fb_verify": fb_verify,
        "fb_webhook": fb_webhook,
        "wa_webhook": wa_webhook,
        "wa_admin_reply": wa_admin_reply,
        "set_pause_by_conv": set_pause_by_conv,
        "chat": chat,
        "wa_internal": wa_internal,
        "chat_history": chat_history,
    }
