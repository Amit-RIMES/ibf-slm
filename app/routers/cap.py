import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.cap_alert import CAPAlert, CAPConfig
from app.models.trigger import TriggerActivation
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_SEVERITIES = ["Extreme", "Severe", "Moderate", "Minor", "Unknown"]
_URGENCIES = ["Immediate", "Expected", "Future", "Past", "Unknown"]
_CERTAINTIES = ["Observed", "Likely", "Possible", "Unlikely", "Unknown"]
_STATUSES = ["Actual", "Test", "Exercise"]
_MSG_TYPES = ["Alert", "Update", "Cancel"]

_SEVERITY_COLOR = {
    "Extreme": "#7f1d1d",
    "Severe": "#dc2626",
    "Moderate": "#f97316",
    "Minor": "#eab308",
    "Unknown": "#6b7280",
}


async def _get_config(db: AsyncSession) -> CAPConfig:
    cfg = await db.scalar(select(CAPConfig).where(CAPConfig.id == 1))
    if not cfg:
        cfg = CAPConfig(id=1)
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


# ── Public endpoints (no auth) ─────────────────────────────────────────────────

@router.get("/cap/feed.xml", include_in_schema=False)
async def cap_atom_feed(db: AsyncSession = Depends(get_db)):
    """Public Atom 1.0 feed of published CAP alerts — for SWIC and other aggregators."""
    from app.core.cap_xml import build_atom_feed

    cfg = await _get_config(db)
    result = await db.execute(
        select(CAPAlert)
        .where(CAPAlert.published == True, CAPAlert.msg_type != "Cancel")  # noqa: E712
        .order_by(desc(CAPAlert.sent))
        .limit(100)
    )
    alerts = result.scalars().all()
    xml = build_atom_feed(alerts, cfg, cfg.base_url)
    return Response(content=xml, media_type="application/atom+xml; charset=utf-8")


@router.get("/cap/alerts/{identifier}.xml", include_in_schema=False)
async def cap_alert_xml(identifier: str, db: AsyncSession = Depends(get_db)):
    """Public individual CAP XML — direct link served in the Atom feed."""
    from app.core.cap_xml import build_cap_xml

    alert = await db.scalar(select(CAPAlert).where(CAPAlert.identifier == identifier))
    if not alert:
        return Response(content="<error>Not found</error>", status_code=404, media_type="application/xml")
    cfg = await _get_config(db)
    xml = build_cap_xml(alert, cfg)
    return Response(content=xml, media_type="application/cap+xml; charset=utf-8")


# ── Admin UI ───────────────────────────────────────────────────────────────────

@router.get("/cap")
async def cap_admin(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        return RedirectResponse("/dashboard")

    cfg = await _get_config(db)
    result = await db.execute(
        select(CAPAlert).order_by(desc(CAPAlert.sent)).limit(100)
    )
    alerts = result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="cap_admin.html",
        context={
            "user": user,
            "cfg": cfg,
            "alerts": alerts,
            "severities": _SEVERITIES,
            "urgencies": _URGENCIES,
            "certainties": _CERTAINTIES,
            "statuses": _STATUSES,
            "severity_color": _SEVERITY_COLOR,
        },
    )


@router.post("/cap/config")
async def cap_save_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    sender_id: str = Form(...),
    sender_name: str = Form(...),
    contact: str = Form(""),
    base_url: str = Form(...),
    language: str = Form("en-US"),
    inbound_feed_url: str = Form(""),
    inbound_enabled: bool = Form(False),
):
    if user.role != "admin":
        return HTMLResponse("Forbidden", status_code=403)

    cfg = await _get_config(db)
    cfg.sender_id = sender_id.strip()
    cfg.sender_name = sender_name.strip()
    cfg.contact = contact.strip() or None
    cfg.base_url = base_url.rstrip("/")
    cfg.language = language.strip()
    cfg.inbound_feed_url = inbound_feed_url.strip() or None
    cfg.inbound_enabled = inbound_enabled
    await db.commit()
    return RedirectResponse("/cap?saved=1", status_code=303)


@router.post("/cap/new")
async def cap_new_alert(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    event: str = Form(...),
    headline: str = Form(...),
    severity: str = Form("Moderate"),
    urgency: str = Form("Expected"),
    certainty: str = Form("Likely"),
    status: str = Form("Actual"),
    area_desc: str = Form(""),
    description: str = Form(""),
    instruction: str = Form(""),
    onset_str: str = Form(""),
    expires_str: str = Form(""),
):
    if user.role != "admin":
        return HTMLResponse("Forbidden", status_code=403)

    from app.core.cap_xml import generate_identifier
    cfg = await _get_config(db)
    now = datetime.now(timezone.utc)

    def _parse_dt(s: str) -> Optional[datetime]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    identifier = generate_identifier(cfg.sender_id)
    # Ensure uniqueness if same day produces duplicate
    existing = await db.scalar(select(CAPAlert).where(CAPAlert.identifier == identifier))
    if existing:
        identifier = identifier + f"-{int(now.timestamp())}"

    alert = CAPAlert(
        identifier=identifier,
        sender=cfg.sender_id,
        sent=now,
        status=status,
        msg_type="Alert",
        scope="Public",
        category="Met",
        event=event,
        urgency=urgency,
        severity=severity,
        certainty=certainty,
        onset=_parse_dt(onset_str) or now,
        expires=_parse_dt(expires_str) or (now + timedelta(hours=48)),
        headline=headline,
        description=description or None,
        instruction=instruction or None,
        web=f"{cfg.base_url}/alerts",
        area_desc=area_desc or None,
        published=False,
    )
    db.add(alert)
    await db.commit()
    return RedirectResponse("/cap", status_code=303)


@router.post("/cap/{alert_id}/edit")
async def cap_edit_alert(
    alert_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    headline: str = Form(...),
    description: str = Form(""),
    instruction: str = Form(""),
    severity: str = Form("Moderate"),
    urgency: str = Form("Expected"),
    certainty: str = Form("Likely"),
    area_desc: str = Form(""),
):
    if user.role != "admin":
        return HTMLResponse("Forbidden", status_code=403)

    alert = await db.scalar(select(CAPAlert).where(CAPAlert.id == alert_id))
    if not alert:
        return HTMLResponse("Not found", status_code=404)

    alert.headline = headline
    alert.description = description or None
    alert.instruction = instruction or None
    alert.severity = severity
    alert.urgency = urgency
    alert.certainty = certainty
    alert.area_desc = area_desc or alert.area_desc
    await db.commit()
    return RedirectResponse("/cap", status_code=303)


@router.post("/cap/{alert_id}/publish")
async def cap_toggle_publish(
    alert_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        return HTMLResponse("Forbidden", status_code=403)

    alert = await db.scalar(select(CAPAlert).where(CAPAlert.id == alert_id))
    if alert:
        alert.published = not alert.published
        await db.commit()
    return RedirectResponse("/cap", status_code=303)


@router.post("/cap/{alert_id}/cancel")
async def cap_cancel_alert(
    alert_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Issue a Cancel message referencing the original Alert."""
    if user.role != "admin":
        return HTMLResponse("Forbidden", status_code=403)

    from app.core.cap_xml import generate_identifier
    original = await db.scalar(select(CAPAlert).where(CAPAlert.id == alert_id))
    if not original:
        return HTMLResponse("Not found", status_code=404)

    cfg = await _get_config(db)
    now = datetime.now(timezone.utc)
    cancel_id = generate_identifier(cfg.sender_id) + "-cancel"

    cancel = CAPAlert(
        identifier=cancel_id,
        sender=cfg.sender_id,
        sent=now,
        status=original.status,
        msg_type="Cancel",
        scope=original.scope,
        category=original.category,
        event=original.event,
        urgency=original.urgency,
        severity=original.severity,
        certainty=original.certainty,
        headline=f"CANCELLED: {original.headline}",
        description=f"This alert has been cancelled.",
        web=original.web,
        area_desc=original.area_desc,
        polygon=original.polygon,
        activation_id=original.activation_id,
        references=f"{original.sender},{original.identifier},{_dt_str(original.sent)}",
        published=True,
    )
    db.add(cancel)
    # Unpublish the original
    original.published = False
    await db.commit()
    return RedirectResponse("/cap", status_code=303)


def _dt_str(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ── Inbound CAP GeoJSON for map layer ──────────────────────────────────────────

_inbound_cache: dict = {"features": [], "fetched_at": None}


@router.get("/map/layers/cap-inbound")
async def cap_inbound_layer(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """GeoJSON FeatureCollection of active external CAP alerts for the map layer."""
    from fastapi.responses import JSONResponse

    cfg = await _get_config(db)
    if not cfg.inbound_enabled or not cfg.inbound_feed_url:
        return JSONResponse({"type": "FeatureCollection", "features": []})

    # Refresh cache at most every 30 minutes
    now = datetime.now(timezone.utc)
    age = (now - _inbound_cache["fetched_at"]).total_seconds() if _inbound_cache["fetched_at"] else 9999
    if age > 1800:
        try:
            features = await _fetch_cap_feed(cfg.inbound_feed_url)
            _inbound_cache["features"] = features
            _inbound_cache["fetched_at"] = now
        except Exception as exc:
            logger.warning("CAP inbound fetch failed: %s", exc)

    return JSONResponse({"type": "FeatureCollection", "features": _inbound_cache["features"]})


async def _fetch_cap_feed(feed_url: str) -> list:
    """Fetch an Atom/RSS CAP feed, parse polygon entries into GeoJSON features."""
    import xml.etree.ElementTree as ET

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(feed_url, follow_redirects=True)
        r.raise_for_status()
        text = r.text

    # Try parsing as Atom/RSS — extract <link type="application/cap+xml"> entries
    root = ET.fromstring(text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cap_ns = "urn:oasis:names:tc:emergency:cap:1.2"

    features = []
    entries = root.findall("atom:entry", ns) or root.findall("entry")

    for entry in entries:
        # Find the CAP XML link
        cap_url = None
        for link in entry.findall("atom:link", ns) or entry.findall("link"):
            if "cap" in (link.get("type") or ""):
                cap_url = link.get("href")
                break

        if not cap_url:
            continue

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                cr = await client.get(cap_url, follow_redirects=True)
                cr.raise_for_status()
                cap_root = ET.fromstring(cr.text)
        except Exception:
            continue

        # Extract from CAP XML
        def _txt(el, tag):
            found = el.find(f"{{{cap_ns}}}{tag}")
            return found.text if found is not None else None

        info = cap_root.find(f"{{{cap_ns}}}info")
        if info is None:
            continue

        area = info.find(f"{{{cap_ns}}}area")
        polygon_str = _txt(area, "polygon") if area is not None else None

        severity = _txt(info, "severity") or "Unknown"
        headline = _txt(info, "headline") or _txt(cap_root, "identifier") or "CAP Alert"
        event = _txt(info, "event") or "Weather Alert"
        expires_str = _txt(info, "expires")

        geometry = None
        if polygon_str:
            try:
                pairs = [p.strip().split(",") for p in polygon_str.strip().split()]
                coords = [[float(p[1]), float(p[0])] for p in pairs]  # CAP is lat,lon → GeoJSON lon,lat
                geometry = {"type": "Polygon", "coordinates": [coords]}
            except Exception:
                pass

        if geometry:
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "headline": headline,
                    "event": event,
                    "severity": severity,
                    "expires": expires_str,
                    "source": "external",
                },
            })

    return features
