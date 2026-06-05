from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

_FORBIDDEN = HTMLResponse(
    "<h1 style='font-family:system-ui;margin:3rem auto;max-width:400px'>403 — Admin access required</h1>",
    status_code=403,
)


@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN

    result = await db.execute(select(User).order_by(User.id))
    users = result.scalars().all()
    return templates.TemplateResponse(
        "admin/users.html", {"request": request, "user": user, "users": users}
    )


@router.post("/users/{target_id}/role")
async def admin_change_role(
    request: Request,
    target_id: int,
    new_role: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    if user.role != "admin":
        return _FORBIDDEN
    if target_id == user.id or new_role not in ("admin", "user"):
        return RedirectResponse("/admin/users", status_code=303)

    result = await db.execute(select(User).where(User.id == target_id))
    target = result.scalar_one_or_none()
    if target:
        target.role = new_role
        await db.commit()
    return RedirectResponse("/admin/users", status_code=303)
