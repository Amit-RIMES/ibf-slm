import hashlib
import ipaddress

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.api_key import APIKey


def _ip_allowed(client_ip: str, allowed_ips: str) -> bool:
    """Return True if client_ip matches any entry in the comma-separated allowed_ips list."""
    try:
        client = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowed_ips.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if client in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if client == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False


async def require_api_key(
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> APIKey:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")
    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    result = await db.execute(
        select(APIKey).where(APIKey.key_hash == key_hash, APIKey.is_active == True)  # noqa: E712
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    # IP allowlist check
    if api_key.allowed_ips:
        client_ip = request.client.host if request.client else ""
        if not _ip_allowed(client_ip, api_key.allowed_ips):
            raise HTTPException(status_code=403, detail=f"Request from {client_ip} is not allowed for this key")

    from datetime import datetime, timezone
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    return api_key
