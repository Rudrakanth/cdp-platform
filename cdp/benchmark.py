"""Tiny benchmark: a 15-condition nested query, cold vs cached, plus the funnel."""
from __future__ import annotations

import statistics
import time

from .service import CDPService

RULE_15 = {"op": "AND", "children": [
    {"kpi": "age", "operator": "between", "value": [25, 55]},
    {"kpi": "loyalty_tier", "operator": "in", "value": ["gold", "platinum"]},
    {"kpi": "spend_total_12m", "operator": "gt", "value": 2000},
    {"kpi": "spend_trend_pct", "operator": "gt", "value": -10},
    {"kpi": "ml_churn_score", "operator": "lt", "value": 0.6},
    {"kpi": "ml_propensity_retail", "operator": "gt", "value": 0.3},
    {"kpi": "ml_next_best_offer", "operator": "ne", "value": "none"},
    {"kpi": "last_purchase_at", "operator": "within_last", "value": {"n": 180, "unit": "day"}},
    {"kpi": "email_opt_in", "operator": "is_true"},
    {"kpi": "browsing_interests", "operator": "contains_any", "value": ["travel", "fashion"]},
    {"kpi": "last_active_location", "operator": "within_radius", "value": {"lat": 25.2048, "lon": 55.2708, "km": 400}},
    {"kpi": "ml_churn_explain", "operator": "json_lt", "value": {"path": "drivers.price", "value": 0.8}},
    {"op": "OR", "children": [
        {"kpi": "has_children", "operator": "is_true"},
        {"kpi": "household_size", "operator": "gte", "value": 4}]},
    {"op": "NOT", "child": {"kpi": "ml_segment_label", "operator": "eq", "value": "dormant"}},
    {"kpi": "ml_top_products_retail", "operator": "is_not_null"},
]}


def run_bench(svc: CDPService, runs: int = 5) -> dict:
    cold = []
    for _ in range(runs):
        t = time.time()
        r = svc.count(RULE_15, use_cache=False)
        cold.append((time.time() - t) * 1000)
    t = time.time()
    svc.count(RULE_15)                     # populate
    warm_t = time.time()
    r = svc.count(RULE_15)
    warm = (time.time() - warm_t) * 1000
    out = {"engine": svc.engine.name, "profiles": r["universe"], "audience": r["count"],
           "cold_ms_median": round(statistics.median(cold), 1), "cold_ms_max": round(max(cold), 1),
           "cached_ms": round(warm, 1), "conditions": r["conditions"]}
    print(out)
    return out
