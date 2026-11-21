"""HTTP layer, read-only paths (shared loaded service, both engines)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cdp.api import create_app
from tests.conftest import NOW

N_GE = {"op": "AND", "children": [{"kpi": "age", "operator": "gte", "value": 30}]}


@pytest.fixture(scope="module")
def client(loaded):
    return TestClient(create_app(loaded, loaded.s))


def count(client, rule, **kw):
    r = client.post("/api/count", json={"rule": rule, "now": NOW.isoformat(), **kw})
    return r


def test_health_meta_and_ui(client):
    assert client.get("/api/health").json()["status"] == "ok"
    m = client.get("/api/meta").json()
    assert set(m["types"]) >= {"number", "string", "bool", "datetime", "list", "geo", "json"}
    assert "within_radius" in m["operators"]["geo"] and m["business_units"]
    ui = client.get("/")
    assert ui.status_code == 200 and "text/html" in ui.headers["content-type"]


def test_kpis_listing_filters(client):
    allk = client.get("/api/kpis").json()
    assert len(allk) >= 59
    travel = client.get("/api/kpis", params={"bu": "TRAVEL"}).json()
    assert travel and all(k["bu"] in ("TRAVEL", "ALL") for k in travel) and len(travel) < len(allk)
    q = client.get("/api/kpis", params={"q": "churn"}).json()
    assert q and all("churn" in k["name"] or "churn" in (k["description"] or "").lower() for k in q)


def test_count_matches_universe_bounds_and_cache(client):
    a = count(client, N_GE).json()
    assert 0 < a["count"] <= a["universe"] and a["cached"] is False
    b = count(client, N_GE).json()
    assert b["count"] == a["count"] and b["cached"] is True
    empty = count(client, {}).json()
    assert empty["count"] == empty["universe"]


def test_count_explain_and_funnel(client):
    rule = {"op": "AND", "children": [{"kpi": "age", "operator": "gte", "value": 30},
                                      {"kpi": "has_children", "operator": "is_true"}]}
    r = count(client, rule, explain=True).json()
    assert r["conditions"] and r["funnel"][-1]["count"] == r["count"]
    assert r["funnel"][0]["count"] >= r["funnel"][-1]["count"]


@pytest.mark.parametrize("rule,code", [
    ({"op": "AND", "children": [{"kpi": "no_such_kpi", "operator": "eq", "value": 1}]}, 404),
    ({"op": "AND", "children": [{"kpi": "age", "operator": "contains", "value": 1}]}, 422),
    ({"op": "AND", "children": [{"kpi": "age", "operator": "gt"}]}, 422),
    ({"op": "AND", "children": [{"kpi": "last_active_city", "operator": "regex", "value": "("}]}, 422),
])
def test_bad_rules_return_4xx_not_500(client, rule, code):
    r = count(client, rule)
    assert r.status_code == code, r.text
    assert r.json()["error"]


def test_bu_visibility_enforced(client):
    rule = {"op": "AND", "children": [{"kpi": "spend_travel_12m", "operator": "gt", "value": 0}]}
    assert count(client, rule, bu="TRAVEL").status_code == 200
    r = count(client, rule, bu="RETAIL")
    assert r.status_code == 422 and "not visible" in r.json()["detail"]


def test_members_and_profile(client):
    r = client.post("/api/members", json={"rule": N_GE, "limit": 5, "now": NOW.isoformat()})
    assert r.status_code == 200
    body = r.json()
    rows = body["customer_ids"]
    assert 0 < len(rows) <= 5 and body["limit"] == 5
    cid = rows[0]
    p = client.get(f"/api/profile/{cid}").json()
    assert p["customer_id"] == cid and p["kpis"]
    assert client.get("/api/profile/does-not-exist").status_code == 404


def test_stats_and_freshness(client):
    s = client.get("/api/stats").json()
    assert s["profiles"] > 0
    assert isinstance(client.get("/api/admin/freshness").json(), (list, dict))
