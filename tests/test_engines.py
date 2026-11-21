"""Every operator, nested trees and injection attempts, on BOTH engines, checked against the
independent Python oracle. If DuckDB, ClickHouse and the oracle all agree, the compiler is right."""
from __future__ import annotations

import random

import pytest

from tests import oracle
from tests.conftest import NOW

L = lambda kpi, operator, value=None: {"kpi": kpi, "operator": operator, **({} if value is None else {"value": value})}
DUBAI = {"lat": 25.2048, "lon": 55.2708}

SPECS = [
    # ---- number
    L("age", "eq", 30), L("age", "ne", 30), L("age", "gt", 50), L("age", "gte", 50), L("age", "lt", 25), L("age", "lte", 25),
    L("age", "between", [30, 40]), L("age", "in", [20, 30, 40]), L("age", "not_in", [20, 30, 40]),
    L("spend_retail_12m", "is_null"), L("spend_retail_12m", "is_not_null"), L("ml_churn_score", "gt", 0.5),
    L("spend_total_12m", "between", [1000, 5000.5]),
    # ---- string (contains/starts/ends are case-insensitive)
    L("gender", "eq", "female"), L("gender", "ne", "female"), L("loyalty_tier", "in", ["gold", "platinum"]),
    L("loyalty_tier", "not_in", ["bronze"]), L("last_active_city", "contains", "AB"), L("last_active_city", "not_contains", "ab"),
    L("last_active_city", "starts_with", "ra"), L("last_active_city", "ends_with", "AI"), L("home_city", "regex", "^(Do|Mu)"),
    L("ml_next_best_offer", "eq", "none"),
    # ---- bool
    L("push_opt_in", "is_true"), L("push_opt_in", "is_false"), L("has_children", "is_true"),
    # ---- datetime
    L("last_purchase_at", "on_date", "2026-10-31"), L("last_purchase_at", "before", "2026-06-01"),
    L("last_purchase_at", "after", "2026-10-01T00:00:00"), L("signup_at", "between", ["2020-01-01", "2020-12-31"]),
    L("signup_at", "between", ["2020-01-01T00:00:00", "2020-06-30T12:30:00"]),
    L("last_purchase_at", "within_last", {"n": 30, "unit": "day"}), L("last_login_at", "within_last", {"n": 12, "unit": "hour"}),
    L("last_login_at", "within_last", {"n": 2, "unit": "week"}), L("last_purchase_at", "not_within_last", {"n": 100, "unit": "day"}),
    L("last_active_at", "is_not_null"),
    # ---- list
    L("browsing_interests", "contains", "travel"), L("browsing_interests", "not_contains", "travel"),
    L("browsing_interests", "contains_any", ["sports", "tech"]), L("browsing_interests", "contains_all", ["sports", "tech"]),
    L("browsing_interests", "size_eq", 2), L("browsing_interests", "size_gt", 3), L("browsing_interests", "size_gte", 3),
    L("browsing_interests", "size_lt", 2), L("browsing_interests", "is_empty"), L("children_age_bands", "is_empty"),
    L("children_age_bands", "contains", "0-2"), L("ml_top_products_retail", "first_is", "RET-0001"),
    L("ml_top_products_retail", "contains_any", ["RET-0002", "RET-0003", "RET-0010"]),
    # ---- geo
    L("last_active_location", "within_radius", {**DUBAI, "km": 30}), L("last_active_location", "within_radius", {**DUBAI, "km": 600}),
    L("last_active_location", "outside_radius", {**DUBAI, "km": 100}),
    L("last_active_location", "within_bbox", {"min_lat": 24.0, "max_lat": 25.0, "min_lon": 54.0, "max_lon": 56.0}),
    # ---- json (nested ML output)
    L("ml_churn_explain", "json_gt", {"path": "drivers.price", "value": 0.5}),
    L("ml_churn_explain", "json_lte", {"path": "drivers.service", "value": 0.2}),
    L("ml_churn_explain", "json_eq", {"path": "model", "value": "churn_v3"}),
    L("ml_churn_explain", "json_ne", {"path": "model", "value": "other"}),
    L("ml_churn_explain", "json_contains", {"path": "model", "value": "CHURN"}),
    L("ml_churn_explain", "json_gt", {"path": "drivers.does_not_exist", "value": 0}),
    L("ml_churn_explain", "json_gt", {"path": "model", "value": 0}),       # number compare on a string -> no match
]


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: f"{s['kpi']}.{s['operator']}.{s.get('value')}"[:70])
def test_leaf_matches_oracle(loaded, core_rows, spec):
    expected = oracle.count(core_rows, spec, NOW)
    got = loaded.count(spec, now=NOW)["count"]
    assert got == expected, f"{loaded.engine.name}: {spec}"


def test_specs_are_not_all_trivial(core_rows):
    counts = [oracle.count(core_rows, s, NOW) for s in SPECS]
    assert sum(1 for c in counts if 0 < c < len(core_rows)) >= len(SPECS) * 0.8   # most leaves are selective


def _rand_node(rng: random.Random, depth: int) -> dict:
    if depth == 0 or rng.random() < 0.45:
        return dict(rng.choice(SPECS))
    kind = rng.choice(["AND", "AND", "OR", "NOT"])
    if kind == "NOT":
        return {"op": "NOT", "child": _rand_node(rng, depth - 1)}
    return {"op": kind, "children": [_rand_node(rng, depth - 1) for _ in range(rng.randint(2, 5))]}


@pytest.mark.parametrize("seed", range(40))
def test_random_nested_trees_match_oracle(loaded, core_rows, seed):
    rng = random.Random(seed)
    rule = _rand_node(rng, 3)
    assert loaded.count(rule, now=NOW)["count"] == oracle.count(core_rows, rule, NOW), (loaded.engine.name, rule)


def test_fifteen_plus_conditions(loaded, core_rows):
    rule = {"op": "AND", "children": [dict(SPECS[i]) for i in (1, 6, 14, 15, 26, 27, 29, 33, 37, 41, 46, 49, 55, 56, 57)]
            + [{"op": "OR", "children": [dict(SPECS[19]), dict(SPECS[21])]}]}
    r = loaded.count(rule, now=NOW, explain=True)
    assert r["conditions"] >= 16
    assert r["count"] == oracle.count(core_rows, rule, NOW)
    counts = [f["count"] for f in r["funnel"]]
    assert counts == sorted(counts, reverse=True)           # a funnel can only shrink
    assert counts[-1] == r["count"]


def test_not_is_the_complement(loaded):
    for spec in SPECS[:12]:
        a = loaded.count(spec, now=NOW)["count"]
        b = loaded.count({"op": "NOT", "child": spec}, now=NOW)["count"]
        assert a + b == loaded.count({}, now=NOW)["universe"]


def test_empty_rule_matches_everyone(loaded):
    r = loaded.count({}, now=NOW)
    assert r["count"] == r["universe"] == 3000
    assert loaded.count({"op": "AND", "children": [{"op": "OR", "children": []}]}, now=NOW)["count"] == 3000


@pytest.mark.parametrize("evil", [
    "x' OR '1'='1", "x'); DROP TABLE profiles; --", "\\' OR 1=1 --", "a\\", "'; SELECT 1; --", "%' OR 1=1 --",
    "x\" OR \"1\"=\"1", "ü'ñ\\'",
])
def test_string_values_cannot_inject_sql(loaded, evil):
    for op in ("eq", "contains", "starts_with"):
        r = loaded.count(L("gender", op, evil), now=NOW)
        assert r["count"] == 0
    assert loaded.count({}, now=NOW)["universe"] == 3000              # tables intact
    assert loaded.count(L("browsing_interests", "contains_any", [evil, "tech"]), now=NOW)["count"] >= 0
    assert loaded.count(L("ml_churn_explain", "json_eq", {"path": "model", "value": evil}), now=NOW)["count"] == 0


@pytest.mark.parametrize("path", ["a;DROP", "a b", "a..b", "$.a", "a'--", "", "1a"])
def test_json_path_is_whitelisted(loaded, path):
    from cdp.errors import ValidationError
    with pytest.raises(ValidationError):
        loaded.count(L("ml_churn_explain", "json_gt", {"path": path, "value": 1}), now=NOW)


def test_bad_regex_is_a_validation_error_not_a_crash(loaded):
    from cdp.errors import ValidationError
    with pytest.raises(ValidationError, match="regular expression"):
        loaded.count(L("gender", "regex", "([unclosed"), now=NOW)
    with pytest.raises(ValidationError, match="regular expression"):
        loaded.members(L("gender", "regex", "([unclosed"), now=NOW)


def test_members_paging_and_consistency(loaded):
    rule = L("loyalty_tier", "eq", "platinum")
    n = loaded.count(rule, now=NOW)["count"]
    all_ids = loaded.members(rule, limit=100_000, now=NOW)["customer_ids"]
    assert len(all_ids) == n and len(set(all_ids)) == n
    page = loaded.members(rule, limit=7, offset=3, now=NOW)["customer_ids"]
    assert page == all_ids[3:10]


def test_profile_lookup_returns_every_type(loaded):
    p = loaded.profile("C000000000")["kpis"]
    assert isinstance(p["age"], float) and isinstance(p["gender"], str) and isinstance(p["browsing_interests"], list)
    assert set(p["last_active_location"]) == {"lat", "lon"} and "drivers" in p["ml_churn_explain"]
    assert "T" in p["last_purchase_at"]
