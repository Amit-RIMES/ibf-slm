"""Country-scope helpers shared by web UI routers and the API layer.

`User.country_scope` is a nullable JSON array of source keys (e.g. "country_bd").
None/empty means unrestricted. These helpers translate that scope into the two
shapes routers need: exact source keys (for ForecastUpload.source) and display
names (for free-text country columns like Station.country / ImpactRecord.country).
"""
import json

from sqlalchemy import false, or_

from app.models.user import User


def allowed_sources(user: User | None) -> list[str] | None:
    """Allowed `source` keys (e.g. "country_bd"), or None if unrestricted."""
    if not user or not user.country_scope:
        return None
    try:
        allowed = json.loads(user.country_scope)
    except Exception:
        return None
    return allowed if allowed else None


def allowed_country_names(user: User | None) -> list[str] | None:
    """Allowed country display names (e.g. "Bangladesh"), for best-effort matching
    against free-text country columns. None if unrestricted."""
    sources = allowed_sources(user)
    if sources is None:
        return None

    from app.routers.forecasts import COUNTRY_NAMES

    names = []
    for s in sources:
        if s.startswith("country_"):
            name = COUNTRY_NAMES.get(s[len("country_"):])
            if name:
                names.append(name)
    return names


def country_condition(column, names: list[str] | None):
    """Best-effort case-insensitive match of a free-text country column against
    `names` (as from `allowed_country_names`). None if unrestricted (no condition
    needed); a never-true condition if restricted but no country names apply."""
    if names is None:
        return None
    if not names:
        return false()
    return or_(*(column.ilike(name) for name in names))
