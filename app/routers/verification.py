from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_action
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.impact import ImpactRecord
from app.models.trigger import TriggerActivation

router = APIRouter(prefix="/verification")
templates = Jinja2Templates(directory="app/templates")

HAZARD_TYPES = ["flood", "storm", "drought", "landslide", "heatwave", "cyclone", "other"]
VARIABLE_LABELS = {
    "precip_mean": "Precip Mean",
    "precip_max": "Precip Max",
    "precip_min": "Precip Min",
    "spi_1": "SPI-1",
    "spi_3": "SPI-3",
    "spi_6": "SPI-6",
}
OPERATOR_SYMBOLS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
_VALID_VERDICTS = {"yes", "partial", "no"}


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("", response_class=HTMLResponse)
async def verification_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    hazard: str = "",
    verdict_filter: str = "",
    date_from: str = "",
    date_to: str = "",
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(TriggerActivation).order_by(desc(TriggerActivation.triggered_at))
    )
    all_activations = result.scalars().all()

    # Map activation_id → list of linked impact IDs
    impact_map: dict[int, list[int]] = {}
    if all_activations:
        act_ids = [a.id for a in all_activations]
        imp_result = await db.execute(
            select(ImpactRecord.trigger_activation_id, ImpactRecord.id)
            .where(ImpactRecord.trigger_activation_id.in_(act_ids))
        )
        for act_id, imp_id in imp_result.all():
            impact_map.setdefault(act_id, []).append(imp_id)

    # Skill metrics computed from ALL activations (not filtered)
    total = len(all_activations)
    n_yes = sum(1 for a in all_activations if a.impact_verdict == "yes")
    n_partial = sum(1 for a in all_activations if a.impact_verdict == "partial")
    n_no = sum(1 for a in all_activations if a.impact_verdict == "no")
    n_verified = n_yes + n_partial + n_no
    n_unverified = total - n_verified
    hit_rate = (n_yes + n_partial) / n_verified if n_verified else None
    far = n_no / n_verified if n_verified else None
    pct_verified = n_verified / total if total else None

    # Apply filters for display
    activations = list(all_activations)
    if hazard:
        activations = [a for a in activations if a.trigger and a.trigger.hazard_type == hazard]
    if verdict_filter == "unverified":
        activations = [a for a in activations if not a.impact_verdict]
    elif verdict_filter in _VALID_VERDICTS:
        activations = [a for a in activations if a.impact_verdict == verdict_filter]

    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            activations = [
                a for a in activations
                if _ensure_aware(a.triggered_at) >= df
            ]
        except ValueError:
            pass

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            activations = [
                a for a in activations
                if _ensure_aware(a.triggered_at) <= dt
            ]
        except ValueError:
            pass

    return templates.TemplateResponse(
        request,
        "verification.html",
        {
            "user": user,
            "activations": activations,
            "impact_map": impact_map,
            "total": total,
            "n_verified": n_verified,
            "n_unverified": n_unverified,
            "n_yes": n_yes,
            "n_partial": n_partial,
            "n_no": n_no,
            "hit_rate": hit_rate,
            "far": far,
            "pct_verified": pct_verified,
            "hazard_types": HAZARD_TYPES,
            "variable_labels": VARIABLE_LABELS,
            "operator_symbols": OPERATOR_SYMBOLS,
            "f_hazard": hazard,
            "f_verdict": verdict_filter,
            "f_date_from": date_from,
            "f_date_to": date_to,
        },
    )


@router.post("/activations/{activation_id}/verdict")
async def set_verdict(
    activation_id: int,
    request: Request,
    verdict: str = Form(...),
    impact_notes: str = Form(""),
    return_qs: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if verdict not in _VALID_VERDICTS:
        qs = f"?{return_qs}" if return_qs else ""
        return RedirectResponse(f"/verification{qs}", status_code=303)

    result = await db.execute(
        select(TriggerActivation).where(TriggerActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if activation:
        activation.impact_verdict = verdict
        activation.impact_notes = impact_notes.strip() or None
        activation.verified_at = datetime.now(timezone.utc)
        await db.commit()
        await log_action(
            db, user.id, "trigger.impact_verified",
            f"Verdict '{verdict}' set for activation {activation_id} ({activation.trigger.name})",
        )

    qs = f"?{return_qs}" if return_qs else ""
    return RedirectResponse(f"/verification{qs}", status_code=303)
