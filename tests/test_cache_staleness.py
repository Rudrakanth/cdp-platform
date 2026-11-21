"""Caching and stale-data protection, on both engines."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import NOW, make_service
from tests.helpers import write_parquet

IDS = [f"c{i}" for i in range(10)]
R = {"op": "AND", "children": [{"kpi": "score", "operator": "gt", "value": 0.5}, {"kpi": "tier", "operator": "eq", "value": "a"}]}


@pytest.fixture
def svc(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "v1.parquet", customer_id=IDS, score=[i / 10 for i in range(10)],
                               tier=["a", "a", "b", "a", "a", "a", "b", "a", "a", "a"], other=[1.0] * 10), model="m")
    return fresh


def test_second_identical_query_is_a_cache_hit(svc):
    a, b = svc.count(R, now=NOW), svc.count(R, now=NOW)
    assert (a["cached"], b["cached"]) == (False, True) and a["count"] == b["count"]


def test_reordered_and_nested_equivalents_hit_the_same_entry(svc):
    svc.count(R, now=NOW)
    flipped = {"op": "AND", "children": list(reversed(R["children"]))}
    nested = {"op": "AND", "children": [R["children"][1], {"op": "AND", "children": [R["children"][0]]}]}
    assert svc.count(flipped, now=NOW)["cached"] and svc.count(nested, now=NOW)["cached"]


def test_use_cache_false_recomputes(svc):
    svc.count(R, now=NOW)
    assert svc.count(R, now=NOW, use_cache=False)["cached"] is False


def test_refreshing_a_used_kpi_invalidates_and_returns_fresh_numbers(svc, tmp_path):
    before = svc.count(R, now=NOW)["count"]
    svc.ingest(write_parquet(tmp_path / "v2.parquet", customer_id=IDS, score=[0.9] * 10), model="m")
    after = svc.count(R, now=NOW)
    assert after["cached"] is False and after["count"] == 8 and after["count"] != before      # 8 profiles have tier 'a'


def test_refreshing_an_unrelated_kpi_keeps_the_cache(svc, tmp_path):
    svc.count(R, now=NOW)
    svc.ingest(write_parquet(tmp_path / "v2.parquet", customer_id=IDS, other=[2.0] * 10), model="m")
    assert svc.count(R, now=NOW)["cached"] is True


def test_upsert_also_invalidates(svc, tmp_path):
    assert svc.count(R, now=NOW)["count"] == 3                  # c7, c8, c9 (c6 has tier b)
    svc.ingest(write_parquet(tmp_path / "d.parquet", customer_id=["c9"], score=[0.0]), model="m", mode="upsert")
    r = svc.count(R, now=NOW)
    assert r["cached"] is False and r["count"] == 2


def test_ttl_is_only_a_safety_net_and_can_expire(fresh, tmp_path):
    fresh.s.cache_ttl_s = 0
    fresh.ingest(write_parquet(tmp_path / "v.parquet", customer_id=IDS, score=[.9] * 10, tier=["a"] * 10), model="m")
    fresh.count(R, now=NOW)
    time.sleep(0.01)
    assert fresh.count(R, now=NOW)["cached"] is False


def test_relative_time_filters_roll_over_with_their_unit(fresh, tmp_path):
    base = datetime(2026, 11, 1, 12, 0)
    fresh.ingest(write_parquet(tmp_path / "t.parquet", customer_id=["a", "b", "c"],
                               seen=[base - timedelta(hours=5), base - timedelta(days=1, hours=1), base - timedelta(days=3)]), model="m")
    day = {"kpi": "seen", "operator": "within_last", "value": {"n": 1, "unit": "day"}}
    r1 = fresh.count(day, now=base)
    assert r1["count"] == 2                                                             # -5h and -1d1h are inside [Oct 31 00:00, now]
    assert fresh.count(day, now=base + timedelta(hours=3))["cached"] is True           # same day bucket -> hit
    r2 = fresh.count(day, now=base + timedelta(days=1))                                 # next day -> recomputed, different answer
    assert r2["cached"] is False and r2["count"] == 1
    hr = {"kpi": "seen", "operator": "within_last", "value": {"n": 6, "unit": "hour"}}
    fresh.count(hr, now=base)
    assert fresh.count(hr, now=base + timedelta(minutes=30))["cached"] is True
    assert fresh.count(hr, now=base + timedelta(hours=1))["cached"] is False


def test_cache_is_bounded(fresh, tmp_path):
    fresh.s.cache_max_entries = 10
    fresh.ingest(write_parquet(tmp_path / "v.parquet", customer_id=IDS, score=[i / 10 for i in range(10)]), model="m")
    for i in range(40):
        fresh.count({"kpi": "score", "operator": "gt", "value": i / 100}, now=NOW)
    assert fresh.reg.cache_stats()["entries"] <= 11


def test_identical_concurrent_queries_compute_once(svc, monkeypatch):
    calls, real = [], svc._count_sql

    def slow(c):
        calls.append(1)
        time.sleep(0.3)
        return real(c)
    monkeypatch.setattr(svc, "_count_sql", slow)
    rule = {"kpi": "score", "operator": "gt", "value": 0.123}
    out = []
    ts = [threading.Thread(target=lambda: out.append(svc.count(rule, now=NOW))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and len({o["count"] for o in out}) == 1 and sum(o["cached"] for o in out) == 7


def test_funnel_reuses_cached_prefixes(svc):
    first = svc.count(R, now=NOW, explain=True)
    again = svc.count(R, now=NOW, explain=True)
    assert [f["cached"] for f in again["funnel"]] == [True, True]
    assert [f["condition"].split()[0] for f in first["funnel"]] == ["score", "tier"]       # user's order, not alphabetical-by-hash


# ------------------------------------------------------------------ stale segments
def test_saved_segment_goes_stale_after_refresh_and_recounts(svc, tmp_path):
    seg = svc.save_segment("hot", R)
    assert svc.get_segment(seg["id"])["status"] == "ok"
    old = svc.get_segment(seg["id"])["last_count"]
    svc.ingest(write_parquet(tmp_path / "v2.parquet", customer_id=IDS, score=[0.9] * 10), model="m")
    s = svc.get_segment(seg["id"])
    assert s["status"] == "stale" and "score refreshed" in s["status_reason"]
    assert [x["id"] for x in svc.refresh_stale_segments()] == [seg["id"]]
    s = svc.get_segment(seg["id"])
    assert s["status"] == "ok" and s["last_count"] == 8 != old


def test_segment_with_unrelated_refresh_stays_ok(svc, tmp_path):
    seg = svc.save_segment("hot", R)
    svc.ingest(write_parquet(tmp_path / "o.parquet", customer_id=IDS, other=[3.0] * 10), model="m")
    assert svc.get_segment(seg["id"])["status"] == "ok"


def test_cannot_save_a_segment_that_does_not_compile(svc):
    from cdp.errors import NotFound, ValidationError
    with pytest.raises(NotFound):
        svc.save_segment("bad", {"kpi": "ghost", "operator": "eq", "value": 1})
    with pytest.raises(ValidationError):
        svc.save_segment("  ", R)


# ------------------------------------------------------------------ freshness SLA
def test_freshness_sla_flags_old_kpis_and_warns_in_counts(fresh, tmp_path):
    fresh.ingest(write_parquet(tmp_path / "v.parquet", customer_id=IDS, score=[.9] * 10), model="m", max_age_hours=24)
    assert fresh.freshness_report() == []
    fresh.reg.db.execute("UPDATE kpis SET loaded_at='2020-01-01T00:00:00Z' WHERE name='score'")
    rep = fresh.freshness_report()
    assert [r["name"] for r in rep] == ["score"] and rep[0]["reason"] == "older than SLA"
    r = fresh.count({"kpi": "score", "operator": "gt", "value": 0.5}, now=NOW)
    assert r["data_as_of"] == "2020-01-01T00:00:00Z" and "score" in r["warnings"][0]
