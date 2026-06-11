"""Ollama client and DB context builder for the chat assistant."""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.forecast import ForecastUpload
from app.models.impact import ImpactRecord
from app.models.trigger import Trigger, TriggerActivation

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an assistant for the IBF (Index-Based Forecasting) \
Slow-onset Livelihood Monitoring application used by RIMES to track climate \
forecasts and their impact on livelihoods across Asia.

You have access to live data from the application (provided below). Use it to \
answer questions accurately and concisely. If the data is insufficient to answer \
a question, say so clearly. Do not fabricate numbers or events.

When referencing forecasts or activations, mention dates and values where relevant.
Keep answers brief and focused — this is an operational tool, not a research report.

{context}"""


async def build_context(db: AsyncSession) -> str:
    """Pull recent app data and format it as a plain-text context block."""
    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)

    # Recent forecasts (last 10)
    fc_result = await db.execute(
        select(ForecastUpload)
        .order_by(desc(ForecastUpload.uploaded_at))
        .limit(10)
    )
    forecasts = fc_result.scalars().all()

    # Active triggers
    trig_result = await db.execute(
        select(Trigger).where(Trigger.is_active == True)  # noqa: E712
    )
    triggers = trig_result.scalars().all()

    # Recent activations (last 30 days)
    act_result = await db.execute(
        select(TriggerActivation)
        .where(TriggerActivation.triggered_at >= cutoff_30d)
        .order_by(desc(TriggerActivation.triggered_at))
        .limit(20)
    )
    activations = act_result.scalars().all()

    # Load trigger names for activation rows
    trig_map: dict[int, str] = {}
    if activations:
        trig_ids = list({a.trigger_id for a in activations})
        tr_result = await db.execute(select(Trigger).where(Trigger.id.in_(trig_ids)))
        for t in tr_result.scalars():
            trig_map[t.id] = t.name

    # Recent impacts (last 30 days)
    imp_result = await db.execute(
        select(ImpactRecord)
        .where(ImpactRecord.created_at >= cutoff_30d)
        .order_by(desc(ImpactRecord.created_at))
        .limit(20)
    )
    impacts = imp_result.scalars().all()

    lines: list[str] = ["=== CURRENT DATA ===", f"(as of {now.strftime('%Y-%m-%d %H:%M UTC')})", ""]

    # Forecasts
    lines.append(f"## Recent Forecasts ({len(forecasts)} shown, most recent first)")
    if forecasts:
        for fc in forecasts:
            lines.append(
                f"  - [{fc.uploaded_at.strftime('%Y-%m-%d')}] {fc.source or 'unknown'} | "
                f"{fc.filename} | precip mean={fc.precip_mean:.1f} max={fc.precip_max:.1f} "
                f"min={fc.precip_min:.1f} mm | period {fc.time_start}–{fc.time_end}"
            )
    else:
        lines.append("  (no forecasts ingested yet)")
    lines.append("")

    # Triggers
    op_sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
    lines.append(f"## Active Trigger Rules ({len(triggers)})")
    if triggers:
        for t in triggers:
            rule = f"{t.variable} {op_sym.get(t.operator, t.operator)} {t.threshold} mm"
            lines.append(f"  - [{t.id}] {t.name} | {t.hazard_type} | {rule}")
    else:
        lines.append("  (no active triggers)")
    lines.append("")

    # Activations
    unacked = [a for a in activations if a.status == "active"]
    lines.append(
        f"## Trigger Activations – last 30 days ({len(activations)} total, "
        f"{len(unacked)} unacknowledged)"
    )
    if activations:
        for a in activations:
            ack = "✓ acknowledged" if a.status == "acknowledged" else "⚠ unacknowledged"
            lines.append(
                f"  - [{a.triggered_at.strftime('%Y-%m-%d')}] {trig_map.get(a.trigger_id, '?')} | "
                f"value={a.value:.2f} mm | {ack}"
            )
    else:
        lines.append("  (no activations in the last 30 days)")
    lines.append("")

    # Impacts
    lines.append(f"## Impact Records – last 30 days ({len(impacts)})")
    if impacts:
        for imp in impacts:
            pop = f", {imp.affected_population:,} affected" if imp.affected_population else ""
            lines.append(
                f"  - [{imp.event_date}] {imp.event_name} | {imp.hazard_type} | "
                f"{imp.country}{pop}"
            )
    else:
        lines.append("  (no impact records in the last 30 days)")

    return "\n".join(lines)


async def stream_chat(
    message: str,
    history: list[dict],
    db: AsyncSession,
) -> AsyncGenerator[str, None]:
    """Stream a chat response from Ollama, yielding SSE-formatted lines."""
    context = await build_context(db)
    system = SYSTEM_PROMPT.format(context=context)

    messages = [{"role": "system", "content": system}]
    for turn in history[-10:]:  # keep last 10 turns to avoid context overflow
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": message})

    payload = {
        "model": settings.OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
    }

    url = f"{settings.OLLAMA_HOST.rstrip('/')}/api/chat"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.error("Ollama error %d: %s", resp.status_code, body)
                    yield f"data: {json.dumps({'error': 'Model unavailable. Is Ollama running?'})}\n\n"
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
        yield f"data: {json.dumps({'error': 'Cannot connect to Ollama. Make sure it is running on ' + settings.OLLAMA_HOST})}\n\n"
    except Exception as exc:
        logger.error("LLM stream error: %s", exc)
        yield f"data: {json.dumps({'error': 'Unexpected error from model.'})}\n\n"
