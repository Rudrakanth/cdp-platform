"""Ingestion modes, atomic publish, failure safety, type inference, retire/purge. Both engines."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from cdp.errors import Conflict, NotFound, ValidationError
from cdp.ingest import infer_type
from tests.conftest import NOW
from tests.helpers import write_parquet

IDS = ["c1", "c2", "c3", "c4", "c5"]
GT = {"kpi": "score", "operator": "gt", "value": 0.25}


def cnt(svc, rule=GT):
    return svc.count(rule, now=NOW)["count"]


def rows_of(svc, kpi_name, table="kpi_num"):
    k = svc.reg.get_kpi(kpi_name, include_retired=True)
    # logical rows: ClickHouse needs FINAL because superseded versions are physically removed only by background merges
    return svc.engine.query(f"SELECT count(*) FROM {svc.engine.dialect.tbl(table)} WHERE kpi_id = {k['kpi_id']}")[0][0]


def test_replace_flips_atomically_and_vacuums_old_generation(fresh, tmp_path):
    fresh.s.gc_grace_s = 0
    fresh.ingest(write_parquet(tmp_path / "v1.parquet", customer_id=IDS, score=[.1, .2, .3, .4, .5]), model="m")
    assert cnt(fresh) == 3
    k1 = fresh.reg.get_kpi("score")
    fresh.ingest(write_parquet(tmp_path / "v2.parquet", customer_id=IDS, score=[.9, .9, .1, .1, .1]), model="m")
    k2 = fresh.reg.get_kpi("score")
    assert cnt(fresh) == 2
    assert k2["active_gen"] > k1["active_gen"] and k2["version"] > k1["version"] and k2["kpi_id"] == k1["kpi_id"]
    assert rows_of(fresh, "score") == 10           # grace period 0, but GC runs on the NEXT write: old gen still present
    assert fresh.gc()["generations_deleted"] == 1
    assert rows_of(fresh, "score") == 5            # the superseded generation was physically removed


def test_superseded_generation_survives_grace_period(fresh, tmp_path):
    """Readers that resolved the old generation just before a flip must still find its data."""
    fresh.s.gc_grace_s = 3600
    fresh.ingest(write_parquet(tmp_path / "v1.parquet", customer_id=IDS, score=[.1, .2, .3, .4, .5]), model="m")
    fresh.ingest(write_parquet(tmp_path / "v2.parquet", customer_id=IDS, score=[.9, .9, .1, .1, .1]), model="m")
    assert fresh.gc()["generations_deleted"] == 0 and fresh.reg.pending_gc_count() == 1
    assert rows_of(fresh, "score") == 10


def test_failed_load_leaves_live_data_and_cleans_orphans(fresh, tmp_path, monkeypatch):
    fresh.ingest(write_parquet(tmp_path / "v1.parquet", customer_id=IDS, score=[.1, .2, .3, .4, .5]), model="m")
    before = fresh.reg.get_kpi("score")

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(fresh.engine, "load_long", boom)
    with pytest.raises(RuntimeError):
        fresh.ingest(write_parquet(tmp_path / "v2.parquet", customer_id=IDS, score=[.9] * 5), model="m")
    monkeypatch.undo()
    after = fresh.reg.get_kpi("score")
    assert (after["active_gen"], after["version"]) == (before["active_gen"], before["version"])
    assert cnt(fresh) == 3                         # readers still see v1
    assert rows_of(fresh, "score") == 5            # no orphan generation left behind


def test_multi_kpi_file_publishes_all_or_nothing(fresh, tmp_path, monkeypatch):
    fresh.ingest(write_parquet(tmp_path / "v1.parquet", customer_id=IDS, score=[.1, .2, .3, .4, .5],
                               tier=["a", "a", "b", "b", "b"]), model="m")
    real, calls = fresh.engine.load_long, []

    def second_type_fails(kt, path, ver):
        calls.append(kt)
        if len(calls) == 2:                        # first type loads, second type blows up
            raise RuntimeError("boom")
        return real(kt, path, ver)
    monkeypatch.setattr(fresh.engine, "load_long", second_type_fails)
    with pytest.raises(RuntimeError):
        fresh.ingest(write_parquet(tmp_path / "v2.parquet", customer_id=IDS, score=[.9] * 5, tier=["z"] * 5), model="m")
    monkeypatch.undo()
    assert cnt(fresh) == 3 and cnt(fresh, {"kpi": "tier", "operator": "eq", "value": "a"}) == 2    # both still on v1
    assert rows_of(fresh, "score") == 5 and rows_of(fresh, "tier", "kpi_str") == 5


def test_replace_drops_absent_profiles_upsert_keeps_them(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "v1.parquet", customer_id=IDS, score=[.9, .9, .9, .1, .1]), model="m")
    assert cnt(fresh) == 3
    fresh.ingest(write_parquet(tmp_path / "d.parquet", customer_id=["c1", "c4"], score=[.1, .9]), model="m", mode="upsert")
    assert cnt(fresh) == 3                         # c1 fell below, c4 rose; c2,c3 untouched
    assert set(fresh.members(GT, now=NOW)["customer_ids"]) == {"c2", "c3", "c4"}
    assert rows_of(fresh, "score") == 5            # upsert never duplicates rows (visible rows == profiles)
    fresh.ingest(write_parquet(tmp_path / "r.parquet", customer_id=["c1", "c2"], score=[.9, .9]), model="m", mode="replace")
    assert set(fresh.members(GT, now=NOW)["customer_ids"]) == {"c1", "c2"}   # c3,c4 lost their value


def test_upsert_into_unloaded_kpi_behaves_like_first_load(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "d.parquet", customer_id=IDS, score=[.9] * 5), model="m", mode="upsert")
    assert cnt(fresh) == 5


def test_nulls_are_absence_not_zero(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "v.parquet", customer_id=IDS, score=[.9, None, .1, None, .9]), model="m")
    assert cnt(fresh, {"kpi": "score", "operator": "is_null"}) == 2
    assert cnt(fresh, {"kpi": "score", "operator": "lt", "value": 0.5}) == 1       # nulls never match comparisons
    assert rows_of(fresh, "score") == 3


def test_type_inference_and_registration(fresh, tmp_path):
    import pyarrow as pa
    t = pa.table({
        "customer_id": IDS,
        "an_int": pa.array([1, 2, 3, 4, 5], pa.int32()), "a_dec": pa.array([1.5] * 5), "a_str": ["x"] * 5,
        "a_bool": [True] * 5, "a_ts": pa.array([datetime(2026, 1, 1)] * 5, pa.timestamp("us")),
        "a_date": pa.array([datetime(2026, 1, 1).date()] * 5), "a_list": [["a", "b"]] * 5, "a_numlist": [[1, 2]] * 5,
        "a_geo": [{"lat": 25.0, "lon": 55.0}] * 5, "a_nested": [{"x": {"y": 1.5}, "z": "s"}] * 5,
        "a_listofobj": [[{"k": 1}]] * 5})
    import pyarrow.parquet as pq
    pq.write_table(t, tmp_path / "t.parquet")
    r = fresh.ingest(str(tmp_path / "t.parquet"), model="m")
    got = {k["name"]: k["type"] for k in r.kpis}
    assert got == {"an_int": "number", "a_dec": "number", "a_str": "string", "a_bool": "bool", "a_ts": "datetime",
                   "a_date": "datetime", "a_list": "list", "a_numlist": "list", "a_geo": "geo", "a_nested": "json",
                   "a_listofobj": "json"}
    assert cnt(fresh, {"kpi": "a_nested", "operator": "json_gt", "value": {"path": "x.y", "value": 1.0}}) == 5
    assert cnt(fresh, {"kpi": "a_numlist", "operator": "contains", "value": "2"}) == 5      # numeric arrays are stored as strings
    assert cnt(fresh, {"kpi": "a_geo", "operator": "within_radius", "value": {"lat": 25, "lon": 55, "km": 1}}) == 5


@pytest.mark.parametrize("duck,expected", [("BIGINT", "number"), ("DECIMAL(18,3)", "number"), ("VARCHAR", "string"),
    ("BOOLEAN", "bool"), ("DATE", "datetime"), ("TIMESTAMP WITH TIME ZONE", "datetime"), ("VARCHAR[]", "list"),
    ("STRUCT(lat DOUBLE, lon DOUBLE)", "geo"), ("STRUCT(lon DOUBLE, lat DOUBLE)", "geo"), ("STRUCT(a DOUBLE, b VARCHAR)", "json"),
    ("STRUCT(a DOUBLE)[]", "json"), ("MAP(VARCHAR, DOUBLE)", "json"), ("JSON", "json")])
def test_infer_type(duck, expected):
    assert infer_type(duck) == expected


def test_csv_and_jsonl_sources(fresh, tmp_path):
    (tmp_path / "a.csv").write_text("customer_id,score,tier,vip\nc1,0.9,gold,true\nc2,0.1,gold,false\nc3,0.8,basic,true\n")
    r = fresh.ingest(str(tmp_path / "a.csv"), model="csvm")
    assert {k["name"]: k["type"] for k in r.kpis} == {"score": "number", "tier": "string", "vip": "bool"}
    assert cnt(fresh) == 2
    (tmp_path / "b.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"customer_id": "c1", "tags": ["a", "b"], "meta": {"s": 0.5}}, {"customer_id": "c9", "tags": ["c"], "meta": {"s": 0.9}}]))
    r = fresh.ingest(str(tmp_path / "b.jsonl"), model="jsonm")
    assert {k["name"]: k["type"] for k in r.kpis} == {"tags": "list", "meta": "json"}
    assert r.profiles_added == 1                    # c9 is new, c1 already known
    assert cnt(fresh, {"kpi": "tags", "operator": "contains", "value": "c"}) == 1


def test_ingest_records_json_api_path(fresh):
    r = fresh.ingest_records([{"customer_id": "a", "score": 0.7, "flags": ["x"]}, {"customer_id": "b", "score": 0.2, "flags": []}],
                             model="live", mode="upsert")
    assert len(r.kpis) == 2 and cnt(fresh) == 1
    assert cnt(fresh, {"kpi": "flags", "operator": "is_empty"}) == 1


def test_add_profiles_false_ignores_unknown_customers(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "a.parquet", customer_id=["c1"], score=[.9]), model="m")
    fresh.ingest(write_parquet(tmp_path / "b.parquet", customer_id=["c1", "zz"], other=[1.0, 2.0]), model="m", add_profiles=False)
    assert fresh.count({}, now=NOW)["universe"] == 1


@pytest.mark.parametrize("make,exc,msg", [
    (lambda p: write_parquet(p, customer_id=["a", "a"], score=[1.0, 2.0]), ValidationError, "duplicate"),
    (lambda p: write_parquet(p, customer_id=["a", None], score=[1.0, 2.0]), ValidationError, "NULL"),
    (lambda p: write_parquet(p, customer_id=["a"], Score=[1.0]), ValidationError, "valid KPI name"),
    (lambda p: write_parquet(p, who=["a"], score=[1.0]), ValidationError, "id column"),
    (lambda p: write_parquet(p, customer_id=["a"]), ValidationError, "no KPI columns"),
])
def test_bad_sources_are_rejected_before_any_state_changes(fresh, tmp_path, make, exc, msg):
    with pytest.raises(exc, match=msg):
        fresh.ingest(make(tmp_path / "bad.parquet"), model="m")
    assert fresh.reg.list_kpis(include_retired=True) == [] and fresh.engine.stats()["profiles"] == 0


def test_type_change_is_a_conflict_and_changes_nothing(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "a.parquet", customer_id=["a"], score=[.9]), model="m")
    with pytest.raises(Conflict, match="registry"):
        fresh.ingest(write_parquet(tmp_path / "b.parquet", customer_id=["a"], score=["high"]), model="m")
    assert cnt(fresh) == 1


def test_model_name_and_extension_required(fresh, tmp_path):
    p = write_parquet(tmp_path / "a.parquet", customer_id=["a"], score=[.9])
    with pytest.raises(ValidationError, match="model"):
        fresh.ingest(p, model="")
    (tmp_path / "x.xlsx").write_text("nope")
    with pytest.raises(ValidationError, match="source must be"):
        fresh.ingest(str(tmp_path / "x.xlsx"), model="m")


def test_retire_is_instant_ids_are_never_reused_and_purge_frees_rows(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "a.parquet", customer_id=IDS, score=[.9] * 5, other=[1.0] * 5), model="old_model")
    seg = fresh.save_segment("hot", GT)
    old_id = fresh.reg.get_kpi("score")["kpi_id"]
    r = fresh.retire_model("old_model")
    assert sorted(r["retired"]) == ["other", "score"] and [s["name"] for s in r["segments_broken"]] == ["hot"]
    with pytest.raises(NotFound):
        fresh.count(GT, now=NOW)                                       # gone for queries immediately...
    assert rows_of(fresh, "score") == 5                                 # ...while the physical delete is deferred
    assert fresh.get_segment(seg["id"])["status"] == "broken"
    assert fresh.purge() == {"purged_kpis": 2}
    assert rows_of(fresh, "score") == 0 and fresh.purge() == {"purged_kpis": 0}
    with pytest.raises(Conflict, match="retired"):
        fresh.ingest(write_parquet(tmp_path / "b.parquet", customer_id=["a"], score=[.9]), model="new")
    fresh.ingest(write_parquet(tmp_path / "c.parquet", customer_id=["a"], score_v2=[.9]), model="new")
    assert fresh.reg.get_kpi("score_v2")["kpi_id"] > old_id


def test_hundreds_of_kpis_in_one_file(fresh, tmp_path):
    cols = {f"ml_k{j:04d}": [float((i * j) % 7) for i in range(50)] for j in range(1, 301)}
    r = fresh.ingest(write_parquet(tmp_path / "big.parquet", customer_id=[f"c{i}" for i in range(50)], **cols), model="bigml")
    assert len(r.kpis) == 300 and fresh.stats()["kpis_active"] == 300
    rule = {"op": "AND", "children": [{"kpi": f"ml_k{j:04d}", "operator": "gte", "value": 1} for j in range(1, 21)]}
    expected = sum(all(((i * j) % 7) >= 1 for j in range(1, 21)) for i in range(50))
    assert fresh.count(rule, now=NOW)["count"] == expected
    assert fresh.retire_model("bigml")["retired"].__len__() == 300
