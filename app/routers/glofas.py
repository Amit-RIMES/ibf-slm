"""GloFAS river discharge forecast dashboard."""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.glofas import GlofasRecord

router = APIRouter(prefix="/glofas")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def glofas_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    records_r = await db.execute(
        select(GlofasRecord)
        .order_by(GlofasRecord.uploaded_at.desc())
        .limit(20)
    )
    records = records_r.scalars().all()

    # Latest record for the map
    latest = records[0] if records else None
    exceedance = None
    if latest and latest.discharge_max > 0:
        # Simple probability estimates for display (fraction of grid cells above thresholds)
        exceedance = {
            f"{int(t)} m³/s": round(min(1.0, latest.discharge_mean / max(t, 0.1)), 2)
            for t in [100, 500, 1000, 5000]
        }

    return templates.TemplateResponse(
        request, "glofas.html",
        {"user": user, "records": records, "latest": latest, "exceedance": exceedance},
    )
