import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.llm import stream_chat
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import TriggerActivation

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login")

    now = datetime.now(timezone.utc)
    month_ago = now - timedelta(days=30)

    unack = await db.scalar(
        select(func.count()).select_from(TriggerActivation)
        .where(TriggerActivation.status == "triggered")
    ) or 0

    recent_impacts = await db.scalar(
        select(func.count()).select_from(ImpactRecord)
        .where(ImpactRecord.event_date >= month_ago.date())
    ) or 0

    latest_fc = await db.scalar(
        select(ForecastUpload.uploaded_at).order_by(ForecastUpload.uploaded_at.desc()).limit(1)
    )

    hints = {
        "unack": unack,
        "recent_impacts": recent_impacts,
        "latest_fc_date": latest_fc.strftime("%Y-%m-%d") if latest_fc else None,
    }
    return templates.TemplateResponse(request, "chat.html", {"user": user, "hints": hints})


@router.post("/api/v1/chat")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return StreamingResponse(
            iter([f"data: {json.dumps({'error': 'Not authenticated'})}\n\n"]),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        stream_chat(body.message, body.history, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
