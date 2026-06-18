"""Ollama client and comprehensive DB context builder for the IBF chat assistant.

Default model: qwen2.5:7b (free, no account needed, runs via Ollama locally).
Change via OLLAMA_MODEL in .env. To switch models:
    ollama pull qwen2.5:7b        # recommended — 8 GB RAM
    ollama pull gemma3:4b         # lighter — 4 GB RAM
    ollama pull phi4              # stronger reasoning — 10 GB RAM
"""
import calendar
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.spi import spi_category
from app.models.bulletin_draft import BulletinDraft
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.observed_rainfall import ObservedRainfall
from app.models.return_level import ReturnLevel
from app.models.risk_history import RiskScoreRecord
from app.models.seasonal import SeasonalForecast
from app.models.spi import SPIRecord
from app.models.station import Station, StationObservation
from app.models.trigger import Trigger, TriggerActivation

logger = logging.getLogger(__name__)

_MONTH_ABBR = [calendar.month_abbr[i] for i in range(1, 13)]
_OP = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "="}

SYSTEM_PROMPT = """You are an expert operational assistant embedded in the IBF-SLM \
(Index-Based Forecasting / Slow-onset Livelihood Monitoring) application, developed \
by RIMES (Regional Integrated Multi-Hazard Early Warning System) for use by National \
Meteorological and Hydrological Services (NMHSs).

Your role is to help duty forecasters and warning officers make fast, confident \
operational decisions based on live data from the application.

GUIDELINES:
- Always cite specific numbers, dates, and data values from the context below.
- Be concise and operational — users are forecasters on shift, not researchers.
- If data is missing or insufficient, say so clearly rather than guessing.
- When a trigger has fired or a risk level is elevated, highlight it prominently.
- Use WMO standard terminology (SPI, return period, exceedance probability, etc.).
- If asked about something not in the data (e.g. a specific location's forecast), \
  say what data IS available and suggest where to look in the app.

{context}"""


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def build_context(db: AsyncSession) -> str:
    """Pull all current app data and format as a structured plain-text context block."""
    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    cutoff_7d  = now - timedelta(days=7)

    lines: list[str] = [
        "=== IBF-SLM OPERATIONAL CONTEXT ===",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    # ── 1. Current risk level ─────────────────────────────────────────────
    latest_risk = await db.scalar(
        select(RiskScoreRecord).order_by(desc(RiskScoreRecord.scored_at)).limit(1)
    )
    lines.append("## 1. CURRENT RISK LEVEL")
    if latest_risk:
        scored = _ensure_aware(latest_risk.scored_at)
        age_h = int((now - scored).total_seconds() / 3600)
        lines.append(
            f"  Level: {latest_risk.level} | Score: {latest_risk.total}/100 "
            f"(last scored {age_h}h ago, source: {latest_risk.source})"
        )
        lines.append(
            f"  Breakdown — SPI: {latest_risk.spi_pts}/40 pts | "
            f"Seasonal: {latest_risk.seasonal_pts}/20 pts | "
            f"Active triggers: {latest_risk.trigger_pts}/40 pts"
        )
        if latest_risk.worst_spi is not None:
            lines.append(f"  Worst SPI contributing: {latest_risk.worst_spi:+.2f}")
    else:
        lines.append("  (no risk score computed yet — run a CHIRPS sync to generate)")
    lines.append("")

    # ── 2. Drought status — SPI ───────────────────────────────────────────
    spi_rows = await db.execute(
        select(SPIRecord)
        .order_by(SPIRecord.source, SPIRecord.timescale, SPIRecord.year, SPIRecord.month)
    )
    all_spi = spi_rows.scalars().all()

    # Group by source → timescale → latest non-null
    spi_latest: dict[str, dict[int, SPIRecord]] = {}
    for rec in all_spi:
        if rec.spi_value is None:
            continue
        src = rec.source
        ts = rec.timescale
        if src not in spi_latest:
            spi_latest[src] = {}
        spi_latest[src][ts] = rec  # later records overwrite earlier → keeps latest

    lines.append("## 2. DROUGHT STATUS — SPI (Standardised Precipitation Index)")
    if spi_latest:
        for src, by_ts in spi_latest.items():
            lines.append(f"  Source: {src}")
            for ts in sorted(by_ts):
                rec = by_ts[ts]
                label, _ = spi_category(rec.spi_value)
                conf = f"low confidence (n={rec.n_reference})" if rec.n_reference < 5 else f"n_ref={rec.n_reference}"
                month_name = _MONTH_ABBR[rec.month - 1]
                lines.append(
                    f"    SPI-{ts}: {rec.spi_value:+.2f} | {label} | "
                    f"{month_name} {rec.year} | {conf}"
                )
    else:
        lines.append("  (no SPI data — upload CHIRPS observations to compute)")
    lines.append("")

    # ── 3. Seasonal outlook ───────────────────────────────────────────────
    latest_sf = await db.scalar(
        select(SeasonalForecast).order_by(desc(SeasonalForecast.issue_date)).limit(1)
    )
    lines.append("## 3. SEASONAL OUTLOOK (latest)")
    if latest_sf:
        terciles = ""
        if latest_sf.below_normal_pct is not None:
            terciles = (
                f"Below normal: {latest_sf.below_normal_pct:.0f}% | "
                f"Near normal: {latest_sf.near_normal_pct:.0f}% | "
                f"Above normal: {latest_sf.above_normal_pct:.0f}%"
            )
        anomaly = (
            f" | Precip anomaly: {latest_sf.precip_anomaly_pct:+.0f}% of climatological normal"
            if latest_sf.precip_anomaly_pct is not None else ""
        )
        lines.append(
            f"  Source: {latest_sf.source} | Issued: {latest_sf.issue_date} | "
            f"Valid: {latest_sf.valid_start} – {latest_sf.valid_end}"
        )
        lines.append(f"  Variable: {latest_sf.variable} | Region: {latest_sf.region_label or 'not specified'}")
        if terciles:
            lines.append(f"  {terciles}{anomaly}")
        if latest_sf.notes:
            lines.append(f"  Notes: {latest_sf.notes}")
    else:
        lines.append("  (no seasonal outlook recorded — add one at /seasonal/new)")
    lines.append("")

    # ── 4. Active trigger rules ───────────────────────────────────────────
    triggers_r = await db.execute(
        select(Trigger).where(Trigger.is_active == True)  # noqa: E712
        .order_by(Trigger.hazard_type, Trigger.name)
    )
    triggers = triggers_r.scalars().all()
    all_triggers_r = await db.execute(select(func.count()).select_from(Trigger))
    total_triggers = all_triggers_r.scalar_one() or 0

    lines.append(f"## 4. TRIGGER RULES ({len(triggers)} active of {total_triggers} total)")
    if triggers:
        for t in triggers:
            op = _OP.get(t.operator, t.operator)
            rule = f"{t.variable} {op} {t.threshold}"
            if t.condition_2_threshold is not None and t.condition_2_operator:
                op2 = _OP.get(t.condition_2_operator, t.condition_2_operator)
                logic = t.logic_op.upper() if t.logic_op else "AND"
                rule += f" {logic} {t.condition_2_variable} {op2} {t.condition_2_threshold}"
            scope = ""
            if t.scope_lat_min is not None:
                scope = f" | bbox: {t.scope_lat_min}–{t.scope_lat_max}°N, {t.scope_lon_min}–{t.scope_lon_max}°E"
            lines.append(f"  [{t.id}] {t.name} | hazard={t.hazard_type} | rule: {rule}{scope}")
    else:
        lines.append("  (no active triggers defined)")
    lines.append("")

    # ── 5. Trigger activations — last 30 days ─────────────────────────────
    act_r = await db.execute(
        select(TriggerActivation, Trigger)
        .join(Trigger, TriggerActivation.trigger_id == Trigger.id)
        .where(TriggerActivation.triggered_at >= cutoff_30d)
        .order_by(desc(TriggerActivation.triggered_at))
        .limit(25)
    )
    act_rows = [(a, t) for a, t in act_r.all()]
    unacked = [a for a, _ in act_rows if a.status == "active"]

    lines.append(
        f"## 5. TRIGGER ACTIVATIONS — last 30 days "
        f"({len(act_rows)} total, {len(unacked)} unacknowledged)"
    )
    if act_rows:
        for act, trig in act_rows:
            ack = "⚠ UNACKNOWLEDGED" if act.status == "active" else "✓ acknowledged"
            verdict = f" | verdict={act.impact_verdict}" if act.impact_verdict else " | verdict=pending"
            fired = _ensure_aware(act.triggered_at).strftime('%Y-%m-%d %H:%M UTC')
            lines.append(
                f"  [{fired}] {trig.name} ({trig.hazard_type}) | "
                f"value={act.value:.2f} | {ack}{verdict}"
            )
    else:
        lines.append("  (no activations in the last 30 days)")
    lines.append("")

    # ── 6. Recent forecasts ───────────────────────────────────────────────
    fc_r = await db.execute(
        select(ForecastUpload)
        .order_by(desc(ForecastUpload.uploaded_at))
        .limit(10)
    )
    forecasts = fc_r.scalars().all()

    lines.append(f"## 6. RECENT FORECASTS (last {len(forecasts)}, most recent first)")
    if forecasts:
        for fc in forecasts:
            uploaded = _ensure_aware(fc.uploaded_at).strftime('%Y-%m-%d')
            anomaly_flag = " ⚠ ANOMALY" if getattr(fc, "is_anomaly", False) else ""
            variable = getattr(fc, "variable", "tp") or "tp"
            lines.append(
                f"  [{uploaded}] {fc.source or 'unknown'} | {variable} | "
                f"mean={fc.precip_mean:.1f} max={fc.precip_max:.1f} min={fc.precip_min:.1f} mm "
                f"| period {fc.time_start}–{fc.time_end}{anomaly_flag}"
            )
    else:
        lines.append("  (no forecasts uploaded yet)")
    lines.append("")

    # ── 7. Return period levels ───────────────────────────────────────────
    rl_r = await db.execute(select(ReturnLevel).order_by(ReturnLevel.variable))
    return_levels = rl_r.scalars().all()

    lines.append("## 7. CLIMATOLOGICAL RETURN PERIOD LEVELS")
    if return_levels:
        for rl in return_levels:
            def _fmt(v):
                return f"{v:.0f}" if v is not None else "—"
            lines.append(
                f"  {rl.variable} ({rl.n_years} yrs of data): "
                f"2yr={_fmt(rl.rl_2)}mm  5yr={_fmt(rl.rl_5)}mm  "
                f"10yr={_fmt(rl.rl_10)}mm  25yr={_fmt(rl.rl_25)}mm  "
                f"50yr={_fmt(rl.rl_50)}mm  100yr={_fmt(rl.rl_100)}mm"
            )
    else:
        lines.append("  (no return levels computed — go to /return-period to compute)")
    lines.append("")

    # ── 8. Weather stations ───────────────────────────────────────────────
    stations_r = await db.execute(
        select(Station).where(Station.is_active == True)  # noqa: E712
        .order_by(Station.name)
        .limit(20)
    )
    stations = stations_r.scalars().all()

    lines.append(f"## 8. WEATHER STATIONS ({len(stations)} active)")
    if stations:
        for st in stations:
            # Get latest observation for this station
            latest_obs = await db.scalar(
                select(StationObservation)
                .where(StationObservation.station_id == st.station_id)
                .order_by(desc(StationObservation.obs_date))
                .limit(1)
            )
            loc = f"{st.lat:.2f}°N, {st.lon:.2f}°E"
            if latest_obs:
                parts = [f"last obs: {latest_obs.obs_date}"]
                if latest_obs.precip_mm is not None:
                    parts.append(f"precip={latest_obs.precip_mm:.1f}mm")
                if latest_obs.temp_max_c is not None:
                    parts.append(f"Tmax={latest_obs.temp_max_c:.1f}°C")
                if latest_obs.temp_min_c is not None:
                    parts.append(f"Tmin={latest_obs.temp_min_c:.1f}°C")
                obs_str = " | ".join(parts)
            else:
                obs_str = "no observations recorded"
            lines.append(f"  {st.name} [{st.station_id}] ({loc}) — {obs_str}")
    else:
        lines.append("  (no weather stations configured)")
    lines.append("")

    # ── 9. Recent impacts ─────────────────────────────────────────────────
    imp_r = await db.execute(
        select(ImpactRecord)
        .where(ImpactRecord.event_date >= cutoff_30d.date())
        .order_by(desc(ImpactRecord.event_date))
        .limit(20)
    )
    impacts = imp_r.scalars().all()

    total_affected = sum(i.affected_population or 0 for i in impacts)
    total_casualties = sum(i.casualties or 0 for i in impacts)

    lines.append(
        f"## 9. RECENT IMPACTS — last 30 days "
        f"({len(impacts)} events | {total_affected:,} affected | {total_casualties} casualties)"
    )
    if impacts:
        for imp in impacts:
            pop = f"{imp.affected_population:,} affected" if imp.affected_population else "population unknown"
            cas = f" | {imp.casualties} casualties" if imp.casualties else ""
            lines.append(
                f"  [{imp.event_date}] {imp.event_name} | {imp.hazard_type} | "
                f"{imp.country} | {pop}{cas}"
            )
    else:
        lines.append("  (no impact records in the last 30 days)")
    lines.append("")

    # ── 10. Bulletin status ───────────────────────────────────────────────
    latest_bulletin = await db.scalar(
        select(BulletinDraft).order_by(desc(BulletinDraft.created_at)).limit(1)
    )
    pending_count = await db.scalar(
        select(func.count()).select_from(BulletinDraft)
        .where(BulletinDraft.status == "pending")
    ) or 0

    lines.append("## 10. BULLETIN STATUS")
    if latest_bulletin:
        created = _ensure_aware(latest_bulletin.created_at).strftime('%Y-%m-%d')
        lines.append(
            f"  Latest draft: \"{latest_bulletin.title or 'Untitled'}\" | "
            f"Status: {latest_bulletin.status} | Risk level: {latest_bulletin.risk_level} | "
            f"Created: {created}"
        )
        if pending_count:
            lines.append(f"  ⚠ {pending_count} pending draft(s) awaiting submission/approval")
    else:
        lines.append("  (no bulletin drafts — generate one at /bulletin)")
    lines.append("")

    # ── 11. Data coverage ─────────────────────────────────────────────────
    last_chirps = await db.scalar(
        select(ObservedRainfall).order_by(desc(ObservedRainfall.obs_date)).limit(1)
    )
    last_forecast = await db.scalar(
        select(ForecastUpload).order_by(desc(ForecastUpload.uploaded_at)).limit(1)
    )

    lines.append("## 11. DATA COVERAGE")
    if last_chirps:
        chirps_age = (now.date() - last_chirps.obs_date).days
        flag = " ⚠ GAP" if chirps_age > 3 else " ✓"
        lines.append(f"  CHIRPS observed rainfall: last record {last_chirps.obs_date} ({chirps_age}d ago){flag}")
    else:
        lines.append("  CHIRPS observed rainfall: ✗ no data ingested")

    if last_forecast:
        fc_age = (now - _ensure_aware(last_forecast.uploaded_at)).days
        flag = " ⚠ GAP" if fc_age > 3 else " ✓"
        lines.append(f"  15-day forecast: last upload {_ensure_aware(last_forecast.uploaded_at).strftime('%Y-%m-%d')} ({fc_age}d ago){flag}")
    else:
        lines.append("  15-day forecast: ✗ no forecasts uploaded")
    lines.append("")

    return "\n".join(lines)


async def stream_chat(
    message: str,
    history: list[dict],
    db: AsyncSession,
) -> AsyncGenerator[str, None]:
    """Stream a chat response from Ollama, yielding SSE-formatted chunks."""
    context = await build_context(db)
    system = SYSTEM_PROMPT.format(context=context)

    messages = [{"role": "system", "content": system}]
    for turn in history[-12:]:  # keep last 12 turns
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": message})

    payload = {
        "model": settings.OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": 0.3,     # lower = more factual, less creative
            "num_ctx": 8192,        # context window — qwen2.5:7b supports up to 32k
        },
    }

    url = f"{settings.OLLAMA_HOST.rstrip('/')}/api/chat"

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.error("Ollama error %d: %s", resp.status_code, body)
                    yield f"data: {json.dumps({'error': 'Model unavailable. Is Ollama running with the correct model?'})}\n\n"
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        text = chunk.get("message", {}).get("content", "")
                        done = chunk.get("done", False)
                        yield f"data: {json.dumps({'chunk': text, 'done': done})}\n\n"
                        if done:
                            return
                    except json.JSONDecodeError:
                        continue
    except httpx.ConnectError:
        yield (
            f"data: {json.dumps({'error': f'Cannot connect to Ollama at {settings.OLLAMA_HOST}. '
                                          f'Run: ollama serve && ollama pull {settings.OLLAMA_MODEL}'})}\n\n"
        )
    except Exception as exc:
        logger.error("LLM stream error: %s", exc)
        yield f"data: {json.dumps({'error': 'Unexpected error from model.'})}\n\n"
