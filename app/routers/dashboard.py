from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.forecast import ForecastUpload
from app.models.user import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    result = await db.execute(select(User).where(User.id == int(payload["sub"])))
    return result.scalar_one_or_none()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    total_users = await db.scalar(select(func.count()).select_from(User))
    total_forecasts = await db.scalar(select(func.count()).select_from(ForecastUpload))

    recent_result = await db.execute(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at)).limit(5)
    )
    recent_forecasts = recent_result.scalars().all()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": {
                "total_users": total_users,
                "total_forecasts": total_forecasts,
                "member_since": user.created_at.strftime("%B %d, %Y"),
            },
            "recent_forecasts": recent_forecasts,
        },
    )
