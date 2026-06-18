from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    from datetime import timezone
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    result = await db.execute(select(User).where(User.id == int(payload["sub"])))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    # Check if all sessions issued before a certain time have been revoked
    invalidated = user.sessions_invalidated_before
    if invalidated is not None:
        iat = payload.get("iat")
        if iat is None:
            return None
        from datetime import datetime
        issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
        if issued_at <= invalidated:
            return None
    return user


def is_admin(user: User | None) -> bool:
    return user is not None and user.role == "admin"
