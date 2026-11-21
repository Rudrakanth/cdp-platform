"""Total-vs-audience breakdown (charts): both engines checked against the Python oracle."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cdp.api import create_app
from cdp.errors import NotFound, ValidationError
from tests import oracle
from tests.conftest import NOW

RULE = {"op": "AND", "children": [{"kpi": "age", "operator": "gte", "value": 30},
                                  {"kpi": "has_children", "operator": "is_true"}]}


def members(rows):
    return [r for r in rows if oracle.evaluate(r, RULE, NOW)]


def test_string_breakdown_matches_oracle(loaded, core_rows):
    r = loaded.breakdown(RULE, "loyalty_tier", now=NOW, limit=20)
    aud = members(core_rows)
    assert r["audience"] == len(aud) and r["universe"] == len(core_rows)
    for g in r["groups"]:
        assert g["audience"] == sum(1 for x in aud if x.get("loyalty_tier") == g["value"])
        assert g["total"] == sum(1 for x in core_rows if x.get("loyalty_tier") == g["value"])
    assert r["other"] is None and r["missing"]["audience"] == r["audience"] - sum(g["audience"] for g in r["groups"])


def test_bool_breakdown_labels_and_totals(loaded, core_rows):
    r = loaded.breakdown(RULE, "email_opt_in", now=NOW)
    assert {g["value"] for g in r["groups"]} <= {"yes", "no"}
    assert sum(g["total"] for g in r["groups"]) + r["missing"]["total"] == len(core_rows)


def test_list_breakdown_counts_profiles_per_element(loaded, core_rows):
    r = loaded.breakdown(RULE, "browsing_interests", now=NOW, limit=20)
    aud = members(core_rows)
    for g in r["groups"]:
        assert g["audience"] == sum(1 for x in aud if g["value"] in (x.get("browsing_interests") or []))
    assert r["missing"] is None


def test_number_breakdown_bins_cover_everything(loaded, core_rows):
    r = loaded.breakdown(RULE, "age", now=NOW)
    assert len(r["groups"]) == 8 and r["other"] is None
    assert sum(g["total"] for g in r["groups"]) + r["missing"]["total"] == len(core_rows)
    assert sum(g["audience"] for g in r["groups"]) + r["missing"]["audience"] == r["audience"]
    assert r["groups"][0]["value"].split("–")[0] == f"{min(x['age'] for x in core_rows if x.get('age') is not None):.4g}"


def test_limit_folds_the_tail_into_other(loaded):
    full = loaded.breakdown(RULE, "home_city", now=NOW, limit=20)
    cut = loaded.breakdown(RULE, "home_city", now=NOW, limit=2)
    assert len(cut["groups"]) == 2 and cut["other"]["n"] == len(full["groups"]) - 2
    assert cut["other"]["total"] + sum(g["total"] for g in cut["groups"]) == sum(g["total"] for g in full["groups"])


def test_empty_rule_means_audience_equals_total(loaded):
    r = loaded.breakdown({}, "loyalty_tier", now=NOW, limit=20)
    assert r["audience"] == r["universe"] and all(g["audience"] == g["total"] for g in r["groups"])


def test_errors(loaded):
    for bad in ("last_active_location", "ml_churn_explain", "last_active_at"):    # geo, json, datetime
        with pytest.raises(ValidationError):
            loaded.breakdown(RULE, bad, now=NOW)
    with pytest.raises(NotFound):
        loaded.breakdown(RULE, "nope", now=NOW)
    with pytest.raises(ValidationError):
        loaded.breakdown(RULE, "loyalty_tier", now=NOW, limit=0)
    with pytest.raises(ValidationError):
        loaded.breakdown({}, "ml_top_products_travel", bu="RETAIL", now=NOW)


def test_endpoint(loaded):
    c = TestClient(create_app(loaded, loaded.s))
    r = c.post("/api/breakdown", json={"rule": RULE, "kpi": "loyalty_tier", "now": NOW.isoformat()})
    assert r.status_code == 200 and r.json()["groups"]
    assert c.post("/api/breakdown", json={"rule": RULE, "kpi": "last_active_location"}).status_code == 422
