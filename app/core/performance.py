"""Compute POD/FAR/CSI for a set of triggers at their current thresholds.

Used by the trigger list page to show inline quality metrics.
"""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.trigger import Trigger

_OPS = {
    "gt":  lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt":  lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
}

FORECAST_VARIABLES = {"precip_mean", "precip_max", "precip_min"}


async def compute_trigger_quality(
    db: "AsyncSession",
    triggers: list["Trigger"],
    match_window: int = 30,
) -> dict[int, dict]:
    """Return {trigger_id: stats} for each trigger.

    Stats: fires, hits, false_alarms, missed, pod, far, csi (percentages or None).
    Only computed for precip-variable triggers — SPI triggers return {}.
    """
    from sqlalchemy import select
    from app.models.forecast import ForecastUpload
    from app.models.impact import ImpactRecord

    precip_triggers = [t for t in triggers if t.variable in FORECAST_VARIABLES]
    if not precip_triggers:
        return {}

    all_forecasts = (
        await db.execute(select(ForecastUpload).order_by(ForecastUpload.uploaded_at))
    ).scalars().all()

    all_impacts = (await db.execute(select(ImpactRecord))).scalars().all()

    # Group impacts by hazard_type for efficient per-trigger filtering
    impacts_by_hazard: dict[str | None, list] = {}
    for imp in all_impacts:
        impacts_by_hazard.setdefault(imp.hazard_type, []).append(imp)
    # None key for triggers with no hazard_type filter — use all impacts
    all_impact_list = list(all_impacts)

    result: dict[int, dict] = {}
    for trigger in precip_triggers:
        relevant_impacts = (
            impacts_by_hazard.get(trigger.hazard_type, [])
            if trigger.hazard_type
            else all_impact_list
        )
        _op = _OPS.get(trigger.operator, lambda v, t: False)

        fc_data = []
        for fc in all_forecasts:
            v = getattr(fc, trigger.variable, None)
            if v is None:
                continue
            fc_date = fc.uploaded_at.date()
            ws = fc_date - timedelta(days=match_window)
            we = fc_date + timedelta(days=match_window)
            has_impact = any(ws <= imp.event_date <= we for imp in relevant_impacts)
            fc_data.append({"value": v, "date": fc_date, "has_impact": has_impact})

        if not fc_data:
            result[trigger.id] = {}
            continue

        imp_windows = []
        for imp in relevant_impacts:
            ws = imp.event_date - timedelta(days=match_window)
            we = imp.event_date + timedelta(days=match_window)
            covering = frozenset(
                i for i, fd in enumerate(fc_data) if ws <= fd["date"] <= we
            )
            imp_windows.append(covering)

        fire_set = {
            i for i, fd in enumerate(fc_data)
            if fd["value"] is not None and _op(fd["value"], trigger.threshold)
        }
        n_fires = len(fire_set)
        n_hits = sum(1 for i in fire_set if fc_data[i]["has_impact"])
        n_fa = n_fires - n_hits
        n_missed = sum(1 for iw in imp_windows if not (iw & fire_set))

        pod = round(n_hits / (n_hits + n_missed) * 100, 1) if (n_hits + n_missed) > 0 else None
        far = round(n_fa / n_fires * 100, 1) if n_fires > 0 else None
        csi = round(n_hits / (n_hits + n_missed + n_fa) * 100, 1) if (n_hits + n_missed + n_fa) > 0 else None

        result[trigger.id] = {
            "total_forecasts": len(fc_data),
            "fires": n_fires,
            "hits": n_hits,
            "false_alarms": n_fa,
            "missed": n_missed,
            "pod": pod,
            "far": far,
            "csi": csi,
        }

    return result
