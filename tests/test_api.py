import pytest
from httpx import AsyncClient


async def test_status_public(client: AsyncClient):
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "alert_count" in data
    assert "alerts" in data


async def test_forecasts_requires_api_key(client: AsyncClient):
    resp = await client.get("/api/v1/forecasts")
    assert resp.status_code == 401


async def test_forecasts_empty(client: AsyncClient, api_key: str):
    resp = await client.get("/api/v1/forecasts", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["data"] == []


async def test_triggers_empty(client: AsyncClient, api_key: str):
    resp = await client.get("/api/v1/triggers", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_create_impact(client: AsyncClient, api_key: str):
    resp = await client.post("/api/v1/impacts", headers={"X-API-Key": api_key}, json={
        "event_name": "Test flood",
        "event_date": "2026-01-15",
        "hazard_type": "flood",
        "country": "Bangladesh",
        "affected_population": 1000,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["event_name"] == "Test flood"
    assert data["hazard_type"] == "flood"
    assert data["id"] is not None
    return data["id"]


async def test_get_impact(client: AsyncClient, api_key: str):
    create_resp = await client.post("/api/v1/impacts", headers={"X-API-Key": api_key}, json={
        "event_name": "Flood X", "event_date": "2026-02-01",
        "hazard_type": "flood", "country": "Nepal",
    })
    impact_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/impacts/{impact_id}", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["id"] == impact_id


async def test_update_impact(client: AsyncClient, api_key: str):
    create_resp = await client.post("/api/v1/impacts", headers={"X-API-Key": api_key}, json={
        "event_name": "Original", "event_date": "2026-03-01",
        "hazard_type": "storm", "country": "Philippines",
    })
    impact_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/v1/impacts/{impact_id}", headers={"X-API-Key": api_key},
                               json={"event_name": "Updated", "casualties": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_name"] == "Updated"
    assert data["casualties"] == 5


async def test_delete_impact(client: AsyncClient, api_key: str):
    create_resp = await client.post("/api/v1/impacts", headers={"X-API-Key": api_key}, json={
        "event_name": "To delete", "event_date": "2026-04-01",
        "hazard_type": "drought", "country": "Kenya",
    })
    impact_id = create_resp.json()["id"]

    del_resp = await client.delete(f"/api/v1/impacts/{impact_id}", headers={"X-API-Key": api_key})
    assert del_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/impacts/{impact_id}", headers={"X-API-Key": api_key})
    assert get_resp.status_code == 404


async def test_create_trigger(client: AsyncClient, api_key: str):
    resp = await client.post("/api/v1/triggers", headers={"X-API-Key": api_key}, json={
        "name": "Test trigger", "hazard_type": "flood",
        "variable": "precip_mean", "operator": "gt", "threshold": 50.0,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test trigger"
    assert data["is_active"] is True


async def test_create_trigger_invalid_variable(client: AsyncClient, api_key: str):
    resp = await client.post("/api/v1/triggers", headers={"X-API-Key": api_key}, json={
        "name": "Bad trigger", "hazard_type": "flood",
        "variable": "not_a_variable", "operator": "gt", "threshold": 50.0,
    })
    assert resp.status_code == 400


async def test_deactivate_trigger(client: AsyncClient, api_key: str):
    create_resp = await client.post("/api/v1/triggers", headers={"X-API-Key": api_key}, json={
        "name": "Deactivate me", "hazard_type": "storm",
        "variable": "precip_max", "operator": "gte", "threshold": 100.0,
    })
    trigger_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/triggers/{trigger_id}", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_impact_invalid_date(client: AsyncClient, api_key: str):
    resp = await client.post("/api/v1/impacts", headers={"X-API-Key": api_key}, json={
        "event_name": "Bad date", "event_date": "not-a-date",
        "hazard_type": "flood", "country": "Test",
    })
    assert resp.status_code == 400
