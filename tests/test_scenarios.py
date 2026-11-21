"""End-to-end scenarios of the sample organisation: a group with 5 BUs, campaign managers, ML refreshes."""
from __future__ import annotations

import pytest

from cdp.datagen import CORE_META, generate_ml_model
from tests import oracle
from tests.conftest import NOW, N_PROFILES
from tests.helpers import write_parquet


@pytest.fixture
def org(fresh, core_path):
    fresh.s.gc_grace_s = 0
    fresh.ingest(core_path, model="core", meta=CORE_META)
    return fresh


def L(kpi, operator, value=None):
    d = {"kpi": kpi, "operator": operator}
    if value is not None:
        d["value"] = value
    return d


def AND(*c): return {"op": "AND", "children": list(c)}
def OR(*c): return {"op": "OR", "children": list(c)}
def NOT(c): return {"op": "NOT", "child": c}


# ---------------------------------------------------------------- 1. travel campaign, 15+ filters, all datatypes
TRAVEL_15 = AND(
    L("age", "between", [25, 55]),
    L("loyalty_tier", "in", ["gold", "platinum", "silver"]),
    L("spend_travel_12m", "gt", 500),
    L("ml_propensity_travel", "gte", 0.3),
    L("ml_churn_score", "lt", 0.8),
    L("email_opt_in", "is_true"),
    L("last_active_at", "within_last", {"n": 365, "unit": "day"}),
    L("last_active_location", "within_radius", {"lat": 25.2048, "lon": 55.2708, "km": 3000}),
    L("browsing_interests", "contains_any", ["travel", "beach", "hotels", "flights", "sports", "music"]),
    OR(L("has_children", "is_true"), L("household_size", "lte", 2)),
    NOT(L("discount_sensitivity", "gt", 0.95)),
    L("device_type", "ne", "feature_phone"),
    L("app_sessions_30d", "gte", 0),
    L("ml_churn_explain", "json_gte", {"path": "drivers.price", "value": 0.05}),
    L("home_city", "is_not_null"),
    L("ml_top_products_travel", "is_not_null"),
    L("purchases_total_count", "gte", 0),
)


def test_travel_campaign_with_16_filters_matches_oracle(org, core_rows):
    assert oracle_leaf_count(TRAVEL_15) >= 15
    res = org.count(TRAVEL_15, bu="TRAVEL", now=NOW, explain=True)
    assert res["count"] == oracle.count(core_rows, TRAVEL_15, NOW)
    assert 0 < res["count"] < res["universe"]
    funnel = [f["count"] for f in res["funnel"]]
    assert funnel == sorted(funnel, reverse=True)            # each added filter can only narrow (AND-only prefix)
    assert res["conditions"] >= 15 and res["elapsed_ms"] < 5000


def oracle_leaf_count(n):
    return 1 if "kpi" in n else sum(oracle_leaf_count(c) for c in ([n["child"]] if n["op"] == "NOT" else n["children"]))


def test_bu_visibility_for_each_business_unit(org):
    for bu in org.s.business_units:
        visible = {k["name"] for k in org.list_kpis(bu=bu)}
        assert f"spend_{bu.lower()}_12m" in visible
        for other in org.s.business_units:
            if other != bu:
                assert f"spend_{other.lower()}_12m" not in visible


# ---------------------------------------------------------------- 2. experiment loop
def test_experiment_loop_monotone_and_cached(org):
    steps = [L("age", "gte", 25), L("spend_total_12m", "gt", 1000), L("ml_churn_score", "lt", 0.5),
             L("email_opt_in", "is_true"), L("has_children", "is_true")]
    counts, rule = [], AND()
    for s in steps:                                           # the UI re-counts after every change
        rule = AND(*rule["children"], s)
        counts.append(org.count(rule, now=NOW)["count"])
    assert counts == sorted(counts, reverse=True) and counts[0] > counts[-1]
    again = org.count(rule, now=NOW)
    assert again["cached"] and again["count"] == counts[-1]
    # a funnel computed once makes every prefix a cache hit (the user re-opens an earlier step)
    org.count(rule, now=NOW, explain=True)
    assert org.count(AND(*steps[:3]), now=NOW)["cached"]


# ---------------------------------------------------------------- 3. save and reuse
def test_save_reuse_segments_across_users(org):
    rule = AND(L("loyalty_tier", "eq", "gold"), L("push_opt_in", "is_true"))
    a = org.save_segment("gold push", rule, owner="ana", bu="RETAIL")
    org.save_segment("other", AND(L("age", "gt", 60)), owner="bo", bu="FASHION")
    assert [s["name"] for s in org.list_segments(owner="ana")] == ["gold push"]
    assert org.get_segment(a["id"])["last_count"] == org.count(rule, now=NOW)["count"]


# ---------------------------------------------------------------- 4. nightly ML refresh makes segments stale
def test_nightly_refresh_marks_stale_then_recounts(org, tmp_path, core_rows):
    rule = AND(L("ml_churn_score", "gt", 0.9))
    seg = org.save_segment("at risk", rule, owner="ana")
    before = org.get_segment(seg["id"])["last_count"]
    ids = [r["customer_id"] for r in core_rows]
    p = write_parquet(tmp_path / "churn.parquet", customer_id=ids, ml_churn_score=[0.95] * len(ids))
    org.ingest(p, model="core", mode="upsert", only=["ml_churn_score"])
    assert org.get_segment(seg["id"])["status"] == "stale"
    assert org.count(rule, now=NOW)["cached"] is False                      # old cache entry cannot be served
    assert org.count(rule, now=NOW)["count"] == len(ids) != before
    org.refresh_stale_segments()
    s = org.get_segment(seg["id"])
    assert s["status"] == "ok" and s["last_count"] == len(ids)


# ---------------------------------------------------------------- 5. a new ML model drops 200 KPIs; retire them at scale
def test_new_model_200_kpis_then_retire_and_purge(org, tmp_path):
    f = str(tmp_path / "m.parquet")
    generate_ml_model(f, "affinity", 200, N_PROFILES)
    org.ingest(f, model="affinity", category="ml_affinity")
    assert len(org.list_kpis(model="affinity")) == 200
    assert org.count(AND(L("ml_affinity_k0001", "gt", 0.5)), now=NOW)["count"] > 0
    retired = org.retire_model("affinity")
    assert org.list_kpis(model="affinity") == []
    assert org.count(AND(L("age", "gte", 18)), now=NOW)["count"] > 0       # the rest of the CDP is untouched
    p = org.purge()
    assert p                                                                 # physical rows removed
    names = {k["name"] for k in org.list_kpis()}
    assert not any(n.startswith("ml_affinity_") for n in names)
    # a retired name is never reused (saved segments must not silently bind to different data)...
    from cdp.errors import Conflict
    with pytest.raises(Conflict):
        org.ingest(f, model="affinity", category="ml_affinity")
    # ...a new model version ships under new names and gets brand-new KPI ids
    f2 = str(tmp_path / "m2.parquet")
    generate_ml_model(f2, "affinity2", 200, N_PROFILES)
    org.ingest(f2, model="affinity2", category="ml_affinity")
    assert len({k["kpi_id"] for k in org.list_kpis(model="affinity2")}) == 200
    assert retired is not None


# ---------------------------------------------------------------- 6. location per city, 7. family analytics
@pytest.mark.parametrize("lat,lon,km", [(25.2048, 55.2708, 40), (24.4539, 54.3773, 40), (51.5074, -0.1278, 40)])
def test_location_radius_matches_oracle(org, core_rows, lat, lon, km):
    rule = AND(L("last_active_location", "within_radius", {"lat": lat, "lon": lon, "km": km}))
    assert org.count(rule, now=NOW)["count"] == oracle.count(core_rows, rule, NOW)


def test_family_analytics_nested(org, core_rows):
    rule = AND(L("has_children", "is_true"),
               OR(L("children_age_bands", "contains", "0-2"), L("children_age_bands", "contains", "3-5")),
               NOT(L("marital_status", "eq", "single")),
               L("family_spend_share", "gt", 0.3))
    assert org.count(rule, now=NOW)["count"] == oracle.count(core_rows, rule, NOW)


# ---------------------------------------------------------------- 9. suggestions follow refreshed data
def test_value_suggestions_see_new_data_after_refresh(org, tmp_path, core_rows):
    assert org.suggest_values("home_city", "zzz")["values"] == []            # cached as "no matches"
    ids = [r["customer_id"] for r in core_rows]
    p = write_parquet(tmp_path / "city.parquet", customer_id=ids, home_city=["Zzzville"] * len(ids))
    org.ingest(p, model="core", mode="upsert", only=["home_city"])
    r = org.suggest_values("home_city", "zzz")
    assert [v["value"] for v in r["values"]] == ["Zzzville"] and r["values"][0]["count"] == len(ids)
