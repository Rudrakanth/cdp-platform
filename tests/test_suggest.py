"""Type-ahead value suggestions for string / list KPIs (both engines, plus the HTTP endpoint)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cdp.api import create_app
from cdp.errors import NotFound, ValidationError


def vals(r):
    return [v["value"] for v in r["values"]]


def test_string_suggestions_are_case_insensitive_and_match_ground_truth(loaded, core_rows):
    r = loaded.suggest_values("home_city", "DUB")
    assert vals(r) == ["Dubai"]
    truth = sum(1 for row in core_rows if row.get("home_city") == "Dubai")
    assert r["values"][0]["count"] == truth


def test_prefix_matches_rank_before_substring_matches(loaded):
    r = loaded.suggest_values("home_city", "a")             # 'Al Ain', 'Abu Dhabi' start with A; others only contain it
    names = vals(r)
    starts = [n for n in names if n.lower().startswith("a")]
    assert starts and names[:len(starts)] == starts


def test_empty_query_returns_most_frequent_first(loaded):
    r = loaded.suggest_values("home_city", "", limit=3)
    counts = [v["count"] for v in r["values"]]
    assert counts == sorted(counts, reverse=True) and len(counts) == 3 and r["truncated"] is True


def test_list_kpi_suggests_individual_elements(loaded, core_rows):
    r = loaded.suggest_values("browsing_interests", "tra")
    assert vals(r) == ["travel"]
    truth = sum(row["browsing_interests"].count("travel") if row.get("browsing_interests") else 0 for row in core_rows)
    assert r["values"][0]["count"] == truth


def test_quotes_and_wildcards_are_treated_as_plain_text(loaded):
    for q in ["'; DROP TABLE kpi_str; --", "%", "_", "\\", "a' OR '1'='1"]:
        assert loaded.suggest_values("home_city", q)["values"] == []
    assert vals(loaded.suggest_values("home_city", "dubai"))      # table still intact


def test_second_call_is_served_from_cache(loaded):
    loaded.suggest_values("last_active_city", "du")
    assert loaded.suggest_values("last_active_city", "du")["cached"] is True


def test_rejects_wrong_types_unknown_kpis_bad_limits_and_other_bu(loaded):
    with pytest.raises(ValidationError):
        loaded.suggest_values("age", "1")                       # number
    with pytest.raises(ValidationError):
        loaded.suggest_values("has_children", "t")              # bool
    with pytest.raises(NotFound):
        loaded.suggest_values("nope", "x")
    with pytest.raises(ValidationError):
        loaded.suggest_values("home_city", "a", limit=0)
    with pytest.raises(ValidationError):
        loaded.suggest_values("ml_top_products_travel", "a", bu="RETAIL")


@pytest.fixture(scope="module")
def client(loaded):
    return TestClient(create_app(loaded, loaded.s))


def test_endpoint(client):
    r = client.get("/api/kpis/home_city/values", params={"q": "abu"})
    assert r.status_code == 200 and [v["value"] for v in r.json()["values"]] == ["Abu Dhabi"]
    assert client.get("/api/kpis/age/values").status_code == 422
    assert client.get("/api/kpis/nope/values").status_code == 404
    assert client.get("/api/kpis/home_city/values", params={"limit": 999}).status_code == 422
