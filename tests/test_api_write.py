"""HTTP layer, mutating paths (fresh service per test, both engines)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cdp.api import create_app
from tests.conftest import NOW
from tests.helpers import write_parquet

IDS = [f"c{i}" for i in range(10)]
R = {"op": "AND", "children": [{"kpi": "score", "operator": "gt", "value": 0.5}]}


def upload(client, path, filename, **params):
    with open(path, "rb") as f:
        return client.post("/api/ingest", params={"filename": filename, **params}, content=f.read())


@pytest.fixture
def client(fresh):
    fresh.s.gc_grace_s = 0
    return TestClient(create_app(fresh, fresh.s))


@pytest.fixture
def loaded_client(client, tmp_path):
    p = write_parquet(tmp_path / "a.parquet", customer_id=IDS, score=[i / 10 for i in range(10)])
    assert upload(client, p, "a.parquet", model="ml_a").status_code == 200
    return client


def test_upload_ingest_then_count(loaded_client):
    r = loaded_client.post("/api/count", json={"rule": R, "now": NOW.isoformat()}).json()
    assert r["count"] == 4 and r["universe"] == 10          # 0.6, 0.7, 0.8, 0.9


def test_upload_rejects_bad_extension_and_bad_file(client, tmp_path):
    p = tmp_path / "x.exe"
    p.write_bytes(b"MZ")
    assert upload(client, p, "x.exe", model="m").status_code == 422
    junk = tmp_path / "junk.parquet"
    junk.write_bytes(b"not parquet")
    assert upload(client, junk, "junk.parquet", model="m").status_code in (400, 422)


def test_json_records_ingest_upsert(loaded_client):
    r = loaded_client.post("/api/ingest/json", json={
        "records": [{"customer_id": "c0", "score": 0.99}], "model": "ml_a", "mode": "upsert"})
    assert r.status_code == 200
    assert loaded_client.post("/api/count", json={"rule": R, "now": NOW.isoformat()}).json()["count"] == 5


def test_ingest_path_is_sandboxed_to_inbox(client, tmp_path):
    outside = write_parquet(tmp_path / "secret.parquet", customer_id=IDS, score=[1.0] * 10)
    r = client.post("/api/ingest/path", json={"path": str(outside), "model": "m"})
    assert r.status_code == 422 and "inbox" in r.json()["detail"]
    r = client.post("/api/ingest/path", json={"path": "../secret.parquet", "model": "m"})
    assert r.status_code == 422


def test_ingest_path_inbox_ok(client, fresh, tmp_path):
    import os
    inbox = os.path.join(fresh.s.data_dir, "inbox")
    os.makedirs(inbox, exist_ok=True)
    write_parquet(os.path.join(inbox, "n.parquet"), customer_id=IDS, score=[0.9] * 10)
    r = client.post("/api/ingest/path", json={"path": "n.parquet", "model": "m"})
    assert r.status_code == 200, r.text


def test_segment_crud_and_refresh_lifecycle(loaded_client, tmp_path):
    c = loaded_client
    seg = c.post("/api/segments", json={"name": "hi", "rule": R, "owner": "ana"}).json()
    sid = seg["id"]
    assert seg["status"] == "ok"
    assert [s["id"] for s in c.get("/api/segments", params={"owner": "ana"}).json()] == [sid]
    # a refresh of the KPI makes the saved segment stale; refreshing it makes it ok again
    p = write_parquet(tmp_path / "b.parquet", customer_id=IDS, score=[0.95] * 10)
    assert upload(c, p, "b.parquet", model="ml_a").status_code == 200
    assert c.get("/api/segments").json()[0]["status"] == "stale"
    out = c.post("/api/segments/refresh-stale").json()
    assert out
    seg2 = c.get("/api/segments").json()[0]
    assert seg2["status"] == "ok" and seg2["last_count"] == 10
    assert c.put(f"/api/segments/{sid}", json={"name": "hi2", "rule": R}).json()["name"] == "hi2"
    assert c.delete(f"/api/segments/{sid}").json() == {"deleted": sid}
    assert c.get("/api/segments").json() == []


def test_retire_breaks_segment_and_purge(loaded_client):
    c = loaded_client
    c.post("/api/segments", json={"name": "s", "rule": R})
    assert c.post("/api/kpis/score/retire").status_code == 200
    assert c.get("/api/segments").json()[0]["status"] == "broken"
    r = c.post("/api/count", json={"rule": R})
    assert r.status_code == 404                              # retired KPI is gone for queries
    assert c.post("/api/kpis/score/retire").status_code in (404, 409)
    assert c.post("/api/admin/purge").status_code == 200
    assert c.get("/api/kpis", params={"include_retired": True}).json()


def test_retire_model_at_scale(client, tmp_path):
    p = write_parquet(tmp_path / "m.parquet", customer_id=IDS, **{f"k{i}": [float(i)] * 10 for i in range(40)})
    assert upload(client, p, "m.parquet", model="big").status_code == 200
    assert len(client.get("/api/kpis", params={"model": "big"}).json()) == 40
    r = client.post("/api/models/big/retire").json()
    assert client.get("/api/kpis", params={"model": "big"}).json() == []
    assert r


def test_api_key_middleware(fresh, monkeypatch):
    monkeypatch.setenv("CDP_API_KEY", "s3cret")
    c = TestClient(create_app(fresh, fresh.s))
    assert c.get("/api/stats").status_code == 401
    assert c.get("/api/stats", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/api/stats", headers={"X-API-Key": "s3cret"}).status_code == 200
