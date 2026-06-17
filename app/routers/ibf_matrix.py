from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import Trigger, TriggerActivation

router = APIRouter(prefix="/ibf")
templates = Jinja2Templates(directory="app/templates")

_PROB_LEVELS = ["Low", "Medium", "High"]
_IMPACT_ASC = ["Negligible", "Minor", "Moderate", "Severe", "Extreme"]

_WMO = {
    "Green":  {"hex": "#16a34a", "bg": "#f0fdf4", "text": "#14532d"},
    "Yellow": {"hex": "#ca8a04", "bg": "#fefce8", "text": "#713f12"},
    "Orange": {"hex": "#ea580c", "bg": "#fff7ed", "text": "#7c2d12"},
    "Red":    {"hex": "#dc2626", "bg": "#fef2f2", "text": "#7f1d1d"},
}

# Standard WMO IBF 5×3 matrix
_MATRIX = {
    ("Extreme",    "Low"):    "Orange",
    ("Extreme",    "Medium"): "Red",
    ("Extreme",    "High"):   "Red",
    ("Severe",     "Low"):    "Yellow",
    ("Severe",     "Medium"): "Orange",
    ("Severe",     "High"):   "Red",
    ("Moderate",   "Low"):    "Green",
    ("Moderate",   "Medium"): "Yellow",
    ("Moderate",   "High"):   "Orange",
    ("Minor",      "Low"):    "Green",
    ("Minor",      "Medium"): "Green",
    ("Minor",      "High"):   "Yellow",
    ("Negligible", "Low"):    "Green",
    ("Negligible", "Medium"): "Green",
    ("Negligible", "High"):   "Green",
}

_HAZARD_ICONS = {
    "flood":   "🌊",
    "storm":   "⛈",
    "drought": "🌵",
    "cyclone": "🌀",
}

_PROB_DESC = {
    "High":   "Currently active (unacknowledged alert)",
    "Medium": "Recent activation (last 7 days) or forecast approaching threshold",
    "Low":    "No recent activation and forecast well below threshold",
}

_IMPACT_DESC = {
    "Extreme":    ">100,000 affected or >100 casualties historically",
    "Severe":     ">10,000 affected or >10 casualties historically",
    "Moderate":   ">1,000 affected or any casualties historically",
    "Minor":      "Small recorded impacts historically",
    "Negligible": "No historical impact data for this hazard type",
}


def _prob_tier(has_active: bool, has_recent: bool, ratio: float | None) -> str:
    if has_active:
        return "High"
    if has_recent or (ratio is not None and ratio >= 0.8):
        return "Medium"
    return "Low"


def _impact_tier(impacts: list) -> tuple[str, int, int]:
    """Returns (tier, count, max_affected)."""
    if not impacts:
        return "Negligible", 0, 0
    affected = [i.affected_population for i in impacts if i.affected_population]
    casualties = sum(i.casualties or 0 for i in impacts)
    max_aff = max(affected) if affected else 0
    if max_aff > 100_000 or casualties > 100:
        tier = "Extreme"
    elif max_aff > 10_000 or casualties > 10:
        tier = "Severe"
    elif max_aff > 1_000 or casualties > 0:
        tier = "Moderate"
    elif max_aff > 0:
        tier = "Minor"
    else:
        tier = "Negligible"
    return tier, len(impacts), max_aff


@router.get("", response_class=HTMLResponse)
async def ibf_matrix(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    triggers = (await db.execute(
        select(Trigger).where(Trigger.is_active == True).order_by(Trigger.hazard_type, Trigger.name)
    )).scalars().all()

    active_acts = (await db.execute(
        select(TriggerActivation).where(TriggerActivation.status == "active")
    )).scalars().all()
    active_ids = {a.trigger_id for a in active_acts}

    recent_acts = (await db.execute(
        select(TriggerActivation).where(TriggerActivation.triggered_at >= seven_days_ago)
    )).scalars().all()
    recent_ids = {a.trigger_id for a in recent_acts}

    latest_fc = (await db.execute(
        select(ForecastUpload).order_by(ForecastUpload.uploaded_at.desc()).limit(1)
    )).scalars().first()

    all_impacts = (await db.execute(select(ImpactRecord))).scalars().all()
    impacts_by_hazard: dict = {}
    for imp in all_impacts:
        impacts_by_hazard.setdefault(imp.hazard_type or "other", []).append(imp)

    # Matrix cell buckets
    buckets: dict = {il: {pl: [] for pl in _PROB_LEVELS} for il in _IMPACT_ASC}

    trigger_list = []
    for t in triggers:
        has_active = t.id in active_ids
        has_recent = t.id in recent_ids

        ratio = None
        if latest_fc and t.variable in ("precip_mean", "precip_max", "precip_min") and t.threshold:
            val = getattr(latest_fc, t.variable, None)
            if val is not None and t.threshold != 0:
                ratio = val / t.threshold

        prob = _prob_tier(has_active, has_recent, ratio)
        impact_tier, impact_count, max_aff = _impact_tier(
            impacts_by_hazard.get(t.hazard_type or "other", [])
        )
        wmo_name = _MATRIX[(impact_tier, prob)]
        wmo = _WMO[wmo_name]

        item = {
            "id": t.id,
            "name": t.name,
            "hazard_type": t.hazard_type or "other",
            "icon": _HAZARD_ICONS.get(t.hazard_type or "other", "⚠"),
            "variable": t.variable,
            "threshold": t.threshold,
            "prob_tier": prob,
            "prob_desc": _PROB_DESC[prob],
            "impact_tier": impact_tier,
            "impact_desc": _IMPACT_DESC[impact_tier],
            "impact_count": impact_count,
            "max_affected": max_aff,
            "has_active": has_active,
            "has_recent": has_recent,
            "wmo_name": wmo_name,
            "hex": wmo["hex"],
            "text": wmo["text"],
            "response_plan": t.response_plan or "",
        }
        trigger_list.append(item)
        buckets[impact_tier][prob].append(item)

    # Build rows for template: Extreme at top
    rows = []
    for il in reversed(_IMPACT_ASC):
        row_cols = []
        for pl in _PROB_LEVELS:
            wmo_name = _MATRIX[(il, pl)]
            wmo = _WMO[wmo_name]
            row_cols.append({
                "prob": pl,
                "impact": il,
                "wmo_name": wmo_name,
                "hex": wmo["hex"],
                "bg": wmo["bg"],
                "text": wmo["text"],
                "triggers": buckets[il][pl],
            })
        rows.append({"label": il, "cols": row_cols})

    return templates.TemplateResponse(request, "ibf_matrix.html", {
        "user": user,
        "rows": rows,
        "prob_levels": _PROB_LEVELS,
        "trigger_list": trigger_list,
        "active_count": len(active_ids),
        "high_risk_count": sum(1 for t in trigger_list if t["wmo_name"] in ("Red", "Orange")),
        "total_triggers": len(triggers),
    })
