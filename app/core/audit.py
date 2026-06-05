from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.audit import AuditLog

ACTION_LABELS = {
    "forecast.import":        "Forecast imported",
    "forecast.delete":        "Forecast deleted",
    "trigger.create":         "Trigger created",
    "trigger.edit":           "Trigger edited",
    "trigger.delete":         "Trigger deleted",
    "trigger.acknowledge":    "Trigger acknowledged",
    "impact.create":          "Impact record created",
    "impact.edit":            "Impact record edited",
    "impact.delete":          "Impact record deleted",
    "user.approve":           "User approved",
    "user.reject":            "Registration rejected",
    "user.role_change":       "User role changed",
    "user.delete":            "User deleted",
    "user.password_change":   "Password changed",
    "user.profile_edit":      "Profile updated",
}


async def log_action(
    db: AsyncSession,
    user_id: Optional[int],
    action: str,
    detail: str = "",
) -> None:
    db.add(AuditLog(user_id=user_id, action=action, detail=detail or None))
    await db.commit()
