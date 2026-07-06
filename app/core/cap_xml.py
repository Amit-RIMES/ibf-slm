"""CAP 1.2 XML generation and Atom feed builder."""
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import Element, SubElement, indent, tostring

_CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"
_ATOM_NS = "http://www.w3.org/2005/Atom"

_HAZARD_EVENT = {
    "flood": "Flood Warning",
    "heavy_rain": "Heavy Rainfall Warning",
    "drought": "Drought Advisory",
    "cyclone": "Tropical Cyclone Warning",
    "storm_surge": "Storm Surge Warning",
    "landslide": "Landslide Warning",
    "heat_wave": "Heat Wave Advisory",
    "strong_wind": "Strong Wind Warning",
}

_SEVERITY_COLORS = {
    "Extreme": "#7f1d1d",
    "Severe": "#dc2626",
    "Moderate": "#f97316",
    "Minor": "#eab308",
    "Unknown": "#6b7280",
}


def event_from_hazard(hazard_type: str) -> str:
    return _HAZARD_EVENT.get(hazard_type.lower(), f"{hazard_type.replace('_', ' ').title()} Alert")


def severity_from_activation(trigger, activation) -> str:
    if activation.probability is not None:
        if activation.probability >= 0.8:
            return "Severe"
        elif activation.probability >= 0.5:
            return "Moderate"
        return "Minor"
    ratio = activation.value / max(abs(trigger.threshold), 0.001)
    if ratio >= 2.0:
        return "Severe"
    elif ratio >= 1.5:
        return "Moderate"
    return "Minor"


def certainty_from_activation(activation) -> str:
    if activation.probability is None:
        return "Observed"
    if activation.probability >= 0.8:
        return "Likely"
    if activation.probability >= 0.5:
        return "Possible"
    return "Unlikely"


def polygon_from_trigger(trigger) -> str | None:
    """Convert trigger bounding box to CAP polygon string (lat,lon pairs)."""
    if not all([
        trigger.scope_lat_min is not None,
        trigger.scope_lat_max is not None,
        trigger.scope_lon_min is not None,
        trigger.scope_lon_max is not None,
    ]):
        return None
    lat1, lat2 = trigger.scope_lat_min, trigger.scope_lat_max
    lon1, lon2 = trigger.scope_lon_min, trigger.scope_lon_max
    # CAP polygon: lat,lon space-separated, closed ring
    return (
        f"{lat1},{lon1} {lat1},{lon2} {lat2},{lon2} {lat2},{lon1} {lat1},{lon1}"
    )


def _dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _el(parent: Element, tag: str, text: str) -> Element:
    el = SubElement(parent, tag)
    el.text = text
    return el


def build_cap_xml(alert, config) -> str:
    """Generate a valid CAP 1.2 XML document for the given CAPAlert."""
    root = Element("alert")
    root.set("xmlns", _CAP_NS)

    _el(root, "identifier", alert.identifier)
    _el(root, "sender", alert.sender)
    _el(root, "sent", _dt(alert.sent))
    _el(root, "status", alert.status)
    _el(root, "msgType", alert.msg_type)
    _el(root, "scope", alert.scope)
    if alert.references:
        _el(root, "references", alert.references)

    info = SubElement(root, "info")
    lang = config.language if config else "en-US"
    _el(info, "language", lang)
    _el(info, "category", alert.category)
    _el(info, "event", alert.event)
    _el(info, "urgency", alert.urgency)
    _el(info, "severity", alert.severity)
    _el(info, "certainty", alert.certainty)

    if alert.onset:
        _el(info, "onset", _dt(alert.onset))
    if alert.expires:
        _el(info, "expires", _dt(alert.expires))

    _el(info, "headline", alert.headline)
    if alert.description:
        _el(info, "description", alert.description)
    if alert.instruction:
        _el(info, "instruction", alert.instruction)
    if alert.web:
        _el(info, "web", alert.web)

    if alert.area_desc or alert.polygon:
        area = SubElement(info, "area")
        _el(area, "areaDesc", alert.area_desc or "Area of concern")
        if alert.polygon:
            _el(area, "polygon", alert.polygon)

    indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode")


def build_atom_feed(alerts: list, config, base_url: str) -> str:
    """Build an Atom 1.0 feed listing published CAP alerts."""
    feed = Element("feed")
    feed.set("xmlns", _ATOM_NS)

    updated = alerts[0].sent if alerts else datetime.now(timezone.utc)

    _el(feed, "id", f"{base_url}/cap/feed.xml")
    _el(feed, "title", f"{config.sender_name if config else 'IBF'} — CAP Alert Feed")
    _el(feed, "updated", _dt(updated))

    author = SubElement(feed, "author")
    _el(author, "name", config.sender_name if config else "IBF Alert System")
    if config and config.contact:
        _el(author, "email", config.contact)

    link_self = SubElement(feed, "link")
    link_self.set("rel", "self")
    link_self.set("href", f"{base_url}/cap/feed.xml")
    link_self.set("type", "application/atom+xml")

    for alert in alerts:
        entry = SubElement(feed, "entry")
        alert_url = f"{base_url}/cap/alerts/{alert.identifier}.xml"
        _el(entry, "id", alert_url)
        _el(entry, "title", alert.headline)
        _el(entry, "updated", _dt(alert.sent))
        _el(entry, "published", _dt(alert.created_at))

        link = SubElement(entry, "link")
        link.set("href", alert_url)
        link.set("type", "application/cap+xml")

        cat = SubElement(entry, "category")
        cat.set("term", alert.severity)
        cat.set("label", f"{alert.severity} — {alert.event}")

        _el(entry, "summary", alert.description or alert.headline)

    indent(feed, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(feed, encoding="unicode")


def generate_identifier(sender_id: str, activation_id: int | None = None) -> str:
    """Generate a unique CAP identifier: sender.YYYYMMDD.activation_id or a counter."""
    from datetime import date
    date_str = date.today().strftime("%Y%m%d")
    suffix = str(activation_id) if activation_id else "manual"
    # Strip non-alphanumeric from sender domain for identifier safety
    safe_sender = sender_id.replace("@", "-").replace(".", "-")
    return f"{safe_sender}.{date_str}.{suffix}"


async def auto_create_cap(activation_id: int, trigger_id: int, forecast_id: int | None) -> None:
    """Called from background after a trigger fires — creates a draft CAPAlert."""
    from app.core.database import AsyncSessionLocal
    from app.models.cap_alert import CAPAlert, CAPConfig
    from app.models.trigger import Trigger, TriggerActivation
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        activation = await db.scalar(select(TriggerActivation).where(TriggerActivation.id == activation_id))
        trigger = await db.scalar(select(Trigger).where(Trigger.id == trigger_id))
        cfg = await db.scalar(select(CAPConfig).where(CAPConfig.id == 1))

        if not activation or not trigger:
            return

        now = datetime.now(timezone.utc)
        sender = cfg.sender_id if cfg else "ibf@example.int"
        base_url = cfg.base_url if cfg else "http://localhost:8000"
        identifier = generate_identifier(sender, activation_id)

        alert = CAPAlert(
            identifier=identifier,
            sender=sender,
            sent=now,
            status="Actual",
            msg_type="Alert",
            scope="Public",
            category="Met",
            event=event_from_hazard(trigger.hazard_type),
            urgency="Expected",
            severity=severity_from_activation(trigger, activation),
            certainty=certainty_from_activation(activation),
            onset=now,
            expires=now + timedelta(hours=48),
            headline=f"{event_from_hazard(trigger.hazard_type)} — {trigger.name}",
            description=(
                f"Trigger '{trigger.name}' has been activated.\n"
                f"Observed value: {activation.value:.2f}"
                + (f" (probability: {activation.probability:.0%})" if activation.probability else "")
                + f"\nThreshold: {trigger.threshold}"
            ),
            instruction=(
                trigger.response_plan
                or "Monitor the situation and follow your standard operating procedures."
            ),
            web=f"{base_url}/alerts",
            area_desc=trigger.name,
            polygon=polygon_from_trigger(trigger),
            activation_id=activation_id,
            published=False,
        )
        db.add(alert)
        await db.commit()
