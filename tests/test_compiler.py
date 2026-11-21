"""Compiler unit tests: validation, canonicalisation, limits, visibility. No engine needed."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from cdp.compiler import Compiler
from cdp.config import Settings
from cdp.engines.dialect import ClickHouseDialect, DuckDialect
from cdp.errors import NotFound, ValidationError
from cdp.registry import Registry

NOW = datetime(2026, 11, 1, 12)


@pytest.fixture(params=[DuckDialect, ClickHouseDialect], ids=["duckdb", "clickhouse"])
def comp(request):
    reg = Registry()
    for name, typ, bu in [("age", "number", "ALL"), ("city", "string", "ALL"), ("opt", "bool", "ALL"), ("seen", "datetime", "ALL"),
                          ("tags", "list", "ALL"), ("loc", "geo", "ALL"), ("blob", "json", "ALL"), ("spend_travel", "number", "TRAVEL"),
                          ("spend_retail", "number", "RETAIL"), ("unloaded", "number", "ALL")]:
        k = reg.register_kpi(name, typ, bu=bu)
        if name != "unloaded":
            reg.publish(k["kpi_id"], reg.allocate_gen(k["kpi_id"]), 10)
    return Compiler(request.param(), reg, Settings())


def leaf(k, op, v=None):
    return {"kpi": k, "operator": op, **({} if v is None else {"value": v})}


def test_unknown_and_unloaded_kpi(comp):
    with pytest.raises(NotFound):
        comp.compile(leaf("nope", "eq", 1), NOW)
    with pytest.raises(ValidationError, match="no data loaded"):
        comp.compile(leaf("unloaded", "gt", 1), NOW)


@pytest.mark.parametrize("kpi,op,val", [
    ("age", "contains", "x"), ("city", "gt", 1), ("opt", "eq", True), ("seen", "contains", "x"), ("tags", "gt", 1),
    ("loc", "eq", 1), ("blob", "eq", 1), ("age", "within_radius", {})])
def test_operator_must_match_type(comp, kpi, op, val):
    with pytest.raises(ValidationError, match="not valid"):
        comp.compile(leaf(kpi, op, val), NOW)


@pytest.mark.parametrize("kpi,op,val", [
    ("age", "gt", "ten"), ("age", "gt", True), ("age", "gt", float("inf")), ("age", "between", [5, 1]), ("age", "between", [1]),
    ("age", "in", []), ("city", "eq", 5), ("city", "regex", ""), ("city", "regex", "x" * 201), ("seen", "within_last", {"n": 0}),
    ("seen", "within_last", {"n": 3, "unit": "year"}), ("seen", "before", "not a date"), ("tags", "size_gt", -1),
    ("tags", "contains_any", []), ("loc", "within_radius", {"lat": 95, "lon": 0, "km": 1}),
    ("loc", "within_radius", {"lat": 0, "lon": 0, "km": 0}), ("loc", "within_bbox", {"min_lat": 5, "max_lat": 1, "min_lon": 0, "max_lon": 1}),
    ("blob", "json_gt", {"path": "a", "value": "str"}), ("blob", "json_gt", {"path": "a;b", "value": 1}), ("age", "gt", None)])
def test_bad_values_are_rejected(comp, kpi, op, val):
    with pytest.raises(ValidationError):
        comp.compile(leaf(kpi, op, val), NOW)


def test_missing_operator_or_value(comp):
    with pytest.raises(ValidationError, match="no operator"):
        comp.compile({"kpi": "age"}, NOW)
    with pytest.raises(ValidationError, match="needs a value"):
        comp.compile({"kpi": "age", "operator": "gt"}, NOW)


def test_canonical_form_is_order_insensitive(comp):
    a, b, c = leaf("age", "gt", 1), leaf("city", "eq", "x"), leaf("opt", "is_true")
    one = comp.compile({"op": "AND", "children": [a, b, c]}, NOW)
    two = comp.compile({"op": "AND", "children": [c, {"op": "AND", "children": [b, a]}, a]}, NOW)   # reordered + nested + duplicate
    assert json.dumps(one.canonical, sort_keys=True) == json.dumps(two.canonical, sort_keys=True)
    assert one.where == two.where


def test_empty_groups_collapse(comp):
    assert comp.compile({}, NOW).where == "1 = 1"
    assert comp.compile({"op": "AND", "children": [{"op": "OR", "children": []}, {"op": "NOT", "children": []}]}, NOW).canonical is None
    single = comp.compile({"op": "OR", "children": [{"op": "AND", "children": [leaf("age", "gt", 3)]}]}, NOW)
    assert single.canonical == leaf("age", "gt", 3)


def test_limits(comp):
    deep = leaf("age", "gt", 1)
    for _ in range(12):
        deep = {"op": "NOT", "child": deep}
    with pytest.raises(ValidationError, match="deeper"):
        comp.compile(deep, NOW)
    many = {"op": "AND", "children": [leaf("age", "gt", i) for i in range(101)]}
    with pytest.raises(ValidationError, match="more than"):
        comp.compile(many, NOW)
    with pytest.raises(ValidationError, match="longer"):
        comp.compile(leaf("age", "in", list(range(10_001))), NOW)
    with pytest.raises(ValidationError):
        comp.compile({"op": "XOR", "children": [leaf("age", "gt", 1)]}, NOW)
    with pytest.raises(ValidationError):
        comp.compile({"op": "NOT", "children": [leaf("age", "gt", 1), leaf("age", "lt", 9)]}, NOW)
    with pytest.raises(ValidationError):
        comp.compile("age > 3", NOW)


def test_business_unit_visibility(comp):
    comp.compile(leaf("spend_travel", "gt", 1), NOW, {"TRAVEL"})
    comp.compile(leaf("age", "gt", 1), NOW, {"TRAVEL"})                      # group-wide KPI is visible to every BU
    with pytest.raises(ValidationError, match="not visible"):
        comp.compile(leaf("spend_retail", "gt", 1), NOW, {"TRAVEL"})
    comp.compile(leaf("spend_retail", "gt", 1), NOW, None)                   # group-level user sees everything


def test_relative_time_is_rounded_to_its_unit(comp):
    a = comp.compile(leaf("seen", "within_last", {"n": 3, "unit": "day"}), datetime(2026, 11, 1, 1))
    b = comp.compile(leaf("seen", "within_last", {"n": 3, "unit": "day"}), datetime(2026, 11, 1, 23))
    assert a.where == b.where and a.rel_unit_s == 86400
    c = comp.compile(leaf("seen", "within_last", {"n": 3, "unit": "day"}), datetime(2026, 11, 2, 1))
    assert a.where != c.where


def test_sql_literals_are_escaped(comp):
    sql = comp.compile(leaf("city", "eq", "o'brien\\"), NOW).where
    expected = {"duckdb": "v = 'o''brien\\'", "clickhouse": "v = 'o\\'brien\\\\'"}[comp.d.name]
    assert expected in sql, sql
    with pytest.raises(ValidationError):
        comp.compile(leaf("city", "eq", "nul\x00byte"), NOW)
