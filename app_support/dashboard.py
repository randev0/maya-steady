from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse


def build_dashboard_router(*, templates, db, require_admin_access, build_pause_action_token, settings):
    router = APIRouter()

    @router.get("/dashboard/analytics", response_class=HTMLResponse)
    async def dashboard_analytics(request: Request):
        analytics_data = await db.get_analytics()
        return templates.TemplateResponse(
            "analytics.html",
            {"request": request, "analytics": analytics_data, "page": "analytics"},
        )

    @router.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        analytics = await db.get_analytics()
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "analytics": analytics, "page": "overview"},
        )

    @router.get("/conversations", response_class=HTMLResponse)
    async def dashboard_conversations(request: Request, page: int = Query(1, ge=1)):
        limit = 20
        offset = (page - 1) * limit
        conversations = await db.list_conversations(limit=limit, offset=offset)
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

    @router.get("/conversations/{conv_id}", response_class=HTMLResponse)
    async def conversation_detail(request: Request, conv_id: UUID):
        detail = await db.get_conversation_detail(conv_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Conversation not found")
        audit = await db.get_facts_audit(detail["user_id"])
        pause = await db.get_pause_state(detail["user_id"])
        return templates.TemplateResponse(
            "conversation_detail.html",
            {
                "request": request,
                "conv": detail,
                "audit": audit,
                "maya_paused": pause["paused"],
                "pause_token": build_pause_action_token(conv_id, True),
                "resume_token": build_pause_action_token(conv_id, False),
                "active_page": "conversations",
            },
        )

    @router.get("/leads", response_class=HTMLResponse)
    async def dashboard_leads(request: Request, page: int = Query(1, ge=1)):
        limit = 20
        offset = (page - 1) * limit
        leads = await db.list_leads(limit=limit, offset=offset)
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

    @router.get("/handoffs", response_class=HTMLResponse)
    async def dashboard_handoffs(request: Request, status: str = Query("pending")):
        handoffs = await db.list_handoffs(status=status)
        return templates.TemplateResponse(
            "handoffs.html",
            {
                "request": request,
                "handoffs": handoffs,
                "filter_status": status,
                "active_page": "handoffs",
            },
        )

    @router.patch("/api/handoffs/{handoff_id}")
    async def update_handoff(handoff_id: UUID, body: dict, _: None = Depends(require_admin_access)):
        status = body.get("status")
        if status not in ("in_progress", "resolved"):
            raise HTTPException(status_code=400, detail="Invalid status")
        assigned_to = body.get("assigned_to")
        await db.update_handoff_status(handoff_id, status, assigned_to)
        return {"ok": True}

    @router.patch("/api/leads/{user_id}/facts")
    async def update_lead_facts(user_id: UUID, body: dict, _: None = Depends(require_admin_access)):
        facts = body.get("facts", {})
        if not facts:
            raise HTTPException(status_code=400, detail="No facts provided")
        updated = await db.update_facts(user_id, facts, changed_by="admin")
        return {"ok": True, "facts": updated.get("facts", {})}

    @router.delete("/api/conversations/{conv_id}")
    async def delete_conversation(conv_id: UUID, _: None = Depends(require_admin_access)):
        await db.delete_conversation(conv_id)
        return {"ok": True}

    @router.get("/api/analytics")
    async def api_analytics():
        return await db.get_analytics()

    @router.get("/api/learning")
    async def api_learning():
        return await db.get_learning_stats()

    @router.get("/learning", response_class=HTMLResponse)
    async def dashboard_learning(request: Request):
        stats = await db.get_learning_stats()
        return templates.TemplateResponse(
            "learning.html",
            {"request": request, "stats": stats, "active_page": "learning"},
        )

    @router.get("/wa-qr", response_class=HTMLResponse)
    async def wa_qr():
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                resp = await client.get(f"{settings.wa_gateway_base_url.rstrip('/')}/qr")
                return HTMLResponse(content=resp.text, status_code=resp.status_code)
            except Exception:
                return HTMLResponse(content="<h2>WA Gateway not running</h2>", status_code=503)

    return router
