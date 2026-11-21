"""Rule tree -> SQL.

Rule format (JSON)::

    {"op": "AND", "children": [
        {"kpi": "ml_churn_score", "operator": "gt", "value": 0.7},
        {"op": "OR", "children": [ {leaf}, {leaf} ]},
        {"op": "NOT", "child": {leaf}}
    ]}

Every leaf becomes ``p.profile_idx [NOT] IN (SELECT profile_idx FROM <typed KPI
table> WHERE kpi_id = ? AND gen = <active gen> AND <predicate>)`` and groups
become AND / OR / NOT around those, so arbitrary nesting costs one hash-set per
leaf. Semantics of negative operators (ne, not_in, not_contains, outside_radius,
not_within_last, json_ne): they match only profiles that HAVE a value for the
KPI. Use a NOT group to also include profiles with no value.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import Settings
from .engines.dialect import Dialect
from .errors import NotFound, ValidationError
from .registry import rule_kpis, Registry
from .types import (JSON_PATH_RE, NO_VALUE, OPERATORS, REL_UNITS, TABLE_FOR)


@dataclass
class Compiled:
    where: str
    kpis: dict = field(default_factory=dict)          # name -> registry row
    canonical: Optional[dict] = None                  # normalized rule (None = match all)
    rel_unit_s: Optional[int] = None                  # finest relative-time unit used, if any
    leaves: int = 0
    has_regex: bool = False


def naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_dt(v: Any) -> tuple[datetime, bool]:
    """Return (datetime, date_only)."""
    if isinstance(v, datetime):
        return naive_utc(v), False
    if not isinstance(v, str) or not v.strip():
        raise ValidationError("datetime value must be an ISO-8601 string")
    s = v.strip().replace("Z", "+00:00")
    try:
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))
        return naive_utc(datetime.fromisoformat(s)), date_only
    except ValueError:
        raise ValidationError(f"invalid datetime '{v}'")


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _need_num(x, what="value"):
    if not _is_num(x):
        raise ValidationError(f"{what} must be a finite number")
    return x


def _need_str(x, what="value", allow_empty=True):
    if not isinstance(x, str) or (not allow_empty and x == ""):
        raise ValidationError(f"{what} must be a {'non-empty ' if not allow_empty else ''}string")
    return x


class Compiler:
    def __init__(self, dialect: Dialect, registry: Registry, settings: Settings):
        self.d, self.reg, self.s = dialect, registry, settings

    # ------------------------------------------------------------ normalize
    def normalize(self, rule: Any) -> Optional[dict]:
        self._leaf_count = 0
        out = self._norm(rule, 1)
        return out

    def _norm(self, n: Any, depth: int) -> Optional[dict]:
        if depth > self.s.max_depth:
            raise ValidationError(f"rule nesting deeper than {self.s.max_depth}")
        if not isinstance(n, dict):
            raise ValidationError("rule nodes must be objects")
        if "kpi" in n:
            self._leaf_count += 1
            if self._leaf_count > self.s.max_leaves:
                raise ValidationError(f"more than {self.s.max_leaves} conditions")
            if "operator" not in n:
                raise ValidationError(f"condition on '{n['kpi']}' has no operator")
            leaf = {"kpi": n["kpi"], "operator": n["operator"]}
            if n.get("value") is not None:
                leaf["value"] = n["value"]
            return leaf
        op = str(n.get("op", "AND")).upper()
        if op == "NOT":
            kids = [n["child"]] if n.get("child") else (n.get("children") or [])
            if len(kids) > 1:
                raise ValidationError("NOT takes exactly one child")
            c = self._norm(kids[0], depth + 1) if kids else None
            return {"op": "NOT", "child": c} if c else None
        if op not in ("AND", "OR"):
            raise ValidationError(f"unknown group op '{op}'")
        flat: list[dict] = []
        for c in n.get("children") or []:
            nc = self._norm(c, depth + 1)
            if nc is None:
                continue
            if nc.get("op") == op:          # flatten (a AND (b AND c)) -> AND(a,b,c)
                flat.extend(nc["children"])
            else:
                flat.append(nc)
        seen, uniq = set(), []
        for c in sorted(flat, key=lambda x: json.dumps(x, sort_keys=True)):   # canonical order: reordering never changes the cache key
            k = json.dumps(c, sort_keys=True)
            if k not in seen:
                seen.add(k)
                uniq.append(c)
        if not uniq:
            return None
        return uniq[0] if len(uniq) == 1 else {"op": op, "children": uniq}

    # -------------------------------------------------------------- compile
    def compile(self, rule: Any, now: datetime, allowed_bus: Optional[set] = None) -> Compiled:
        canon = self.normalize(rule)
        c = Compiled(where="1 = 1", canonical=canon)
        if canon is not None:
            self._allowed = {b.upper() for b in allowed_bus} | {"ALL"} if allowed_bus else None
            self._now = naive_utc(now)
            # ONE atomic read of every KPI this rule uses: a concurrent refresh can never give us the new
            # generation of one KPI and the old generation of another.
            self._snap = self.reg.get_kpis(rule_kpis(canon))
            c.where = self._node(canon, c)
        return c

    def _node(self, n: dict, c: Compiled) -> str:
        if "kpi" in n:
            c.leaves += 1
            return self._leaf(n, c)
        if n["op"] == "NOT":
            return f"NOT ({self._node(n['child'], c)})"
        glue = " AND " if n["op"] == "AND" else " OR "
        return "(" + glue.join(self._node(x, c) for x in n["children"]) + ")"

    def _leaf(self, n: dict, c: Compiled) -> str:
        name = n["kpi"]
        k = self._snap.get(name) if isinstance(name, str) else None
        if not k:
            raise NotFound(f"unknown or retired KPI '{name}'")
        if self._allowed is not None and k["bu"] not in self._allowed:
            raise ValidationError(f"KPI '{name}' belongs to {k['bu']} and is not visible here")
        if k["active_gen"] == 0:
            raise ValidationError(f"KPI '{name}' has no data loaded yet")
        op = n["operator"]
        if op not in OPERATORS[k["type"]]:
            raise ValidationError(f"operator '{op}' is not valid for {k['type']} KPI '{name}'; "
                                  f"use one of {', '.join(OPERATORS[k['type']])}")
        c.kpis[name] = k
        base = (f"SELECT profile_idx FROM {self.d.tbl(TABLE_FOR[k['type']])} "
                f"WHERE kpi_id = {int(k['kpi_id'])} AND gen = {int(k['active_gen'])}")
        if op == "is_null":
            return f"p.profile_idx NOT IN ({base})"
        if op == "is_not_null":
            return f"p.profile_idx IN ({base})"
        if op in NO_VALUE:
            cond = self._no_value(k["type"], op)
        else:
            if "value" not in n:
                raise ValidationError(f"operator '{op}' on '{name}' needs a value")
            cond = getattr(self, f"_c_{k['type']}")(op, n["value"], c)
        return f"p.profile_idx IN ({base} AND {cond})"

    # -------------------------------------------------------- type handlers
    def _no_value(self, t: str, op: str) -> str:
        if t == "bool":
            return "v = true" if op == "is_true" else "v = false"
        if t == "list" and op == "is_empty":
            return f"{self.d.list_len('v')} = 0"
        raise ValidationError(f"operator {op} is not valid for {t}")

    def _in_list(self, xs, conv):
        if not isinstance(xs, list) or not xs:
            raise ValidationError("value must be a non-empty list")
        if len(xs) > self.s.max_in_list:
            raise ValidationError(f"list longer than {self.s.max_in_list}")
        return [conv(x) for x in xs]

    def _c_number(self, op, v, c):
        d = self.d
        cmp_ = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        if op in cmp_:
            return f"v {cmp_[op]} {d.num(_need_num(v))}"
        if op == "between":
            if not (isinstance(v, list) and len(v) == 2):
                raise ValidationError("between needs [low, high]")
            lo, hi = _need_num(v[0], "low"), _need_num(v[1], "high")
            if lo > hi:
                raise ValidationError("between: low must be <= high")
            return f"v >= {d.num(lo)} AND v <= {d.num(hi)}"
        if op in ("in", "not_in"):
            xs = ", ".join(d.num(x) for x in self._in_list(v, _need_num))
            return f"v {'NOT ' if op == 'not_in' else ''}IN ({xs})"
        raise ValidationError(op)

    def _c_string(self, op, v, c):
        d = self.d
        if op in ("in", "not_in"):
            xs = ", ".join(d.str(x) for x in self._in_list(v, _need_str))
            return f"v {'NOT ' if op == 'not_in' else ''}IN ({xs})"
        s = _need_str(v, allow_empty=op in ("eq", "ne", "contains", "not_contains", "starts_with", "ends_with"))
        if op == "eq":
            return f"v = {d.str(s)}"
        if op == "ne":
            return f"v <> {d.str(s)}"
        if op == "contains":
            return d.contains_ci("v", s)
        if op == "not_contains":
            return f"NOT ({d.contains_ci('v', s)})"
        if op == "starts_with":
            return d.starts_ci("v", s)
        if op == "ends_with":
            return d.ends_ci("v", s)
        if op == "regex":
            if len(s) > 200:
                raise ValidationError("regex longer than 200 characters")
            c.has_regex = True
            return d.regex("v", s)
        raise ValidationError(op)

    def _c_bool(self, op, v, c):
        raise ValidationError(f"operator {op} is not valid for bool")

    def _rel(self, v, c: Compiled) -> datetime:
        if not isinstance(v, dict):
            raise ValidationError('relative time needs {"n": <int>, "unit": "day"}')
        n, unit = v.get("n"), v.get("unit", "day")
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0 or n > 100_000:
            raise ValidationError("n must be a positive integer")
        if unit not in REL_UNITS:
            raise ValidationError(f"unit must be one of {', '.join(REL_UNITS)}")
        sec = REL_UNITS[unit]
        c.rel_unit_s = sec if c.rel_unit_s is None else min(c.rel_unit_s, sec)
        epoch = self._now.replace(tzinfo=timezone.utc).timestamp()
        floor = math.floor(epoch / sec) * sec          # round "now" down to the unit: SQL and cache key agree
        return datetime.fromtimestamp(floor - n * sec, tz=timezone.utc).replace(tzinfo=None)

    def _c_datetime(self, op, v, c):
        d = self.d
        if op in ("within_last", "not_within_last"):
            cutoff = self._rel(v, c)
            return f"v {'>=' if op == 'within_last' else '<'} {d.ts(cutoff)}"
        if op == "between":
            if not (isinstance(v, list) and len(v) == 2):
                raise ValidationError("between needs [start, end]")
            (a, _), (b, b_date) = parse_dt(v[0]), parse_dt(v[1])
            if b_date:      # date-only end bound is inclusive of the whole day
                return f"v >= {d.ts(a)} AND v < {d.ts(b + timedelta(days=1))}"
            return f"v >= {d.ts(a)} AND v <= {d.ts(b)}"
        t, _ = parse_dt(v)
        if op == "on_date":
            day = t.replace(hour=0, minute=0, second=0, microsecond=0)
            return f"v >= {d.ts(day)} AND v < {d.ts(day + timedelta(days=1))}"
        if op == "before":
            return f"v < {d.ts(t)}"
        if op == "after":
            return f"v >= {d.ts(t)}"
        raise ValidationError(op)

    def _c_list(self, op, v, c):
        d = self.d
        if op in ("contains", "not_contains"):
            e = d.list_has("v", _need_str(v))
            return e if op == "contains" else f"NOT ({e})"
        if op in ("contains_any", "contains_all"):
            xs = self._in_list(v, _need_str)
            return d.list_any("v", xs) if op == "contains_any" else d.list_all("v", xs)
        if op.startswith("size_"):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValidationError("size must be a non-negative integer")
            sym = {"size_eq": "=", "size_gt": ">", "size_gte": ">=", "size_lt": "<"}[op]
            return f"{d.list_len('v')} {sym} {int(v)}"
        if op == "first_is":
            return f"{d.list_first('v')} = {d.str(_need_str(v, allow_empty=False))}"
        raise ValidationError(op)

    @staticmethod
    def _lat_lon(v, who):
        if not isinstance(v, dict):
            raise ValidationError(f"{who} needs an object")
        lat, lon = _need_num(v.get("lat"), "lat"), _need_num(v.get("lon"), "lon")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValidationError("lat/lon out of range")
        return lat, lon

    def _c_geo(self, op, v, c):
        d = self.d
        if op in ("within_radius", "outside_radius"):
            lat, lon = self._lat_lon(v, op)
            km = _need_num(v.get("km"), "km")
            if not (0 < km <= 20_000):
                raise ValidationError("km must be in (0, 20000]")
            if op == "within_radius":
                # bounding box first (prunes by min/max statistics), exact haversine second
                dlat = km / 111.32
                dlon = km / max(111.32 * math.cos(math.radians(lat)), 1e-6)
                box = (f"lat BETWEEN {d.num(lat - dlat)} AND {d.num(lat + dlat)}"
                       if abs(lat) + dlat < 90 and dlon < 180 else "1 = 1")
                if box != "1 = 1":
                    lo, hi = lon - dlon, lon + dlon
                    if -180 <= lo and hi <= 180:
                        box += f" AND lon BETWEEN {d.num(lo)} AND {d.num(hi)}"
                return f"{box} AND {d.distance_km('lat', 'lon', lat, lon)} <= {d.num(km)}"
            return f"{d.distance_km('lat', 'lon', lat, lon)} > {d.num(km)}"
        if op == "within_bbox":
            if not isinstance(v, dict):
                raise ValidationError("within_bbox needs an object")
            a, b = _need_num(v.get("min_lat"), "min_lat"), _need_num(v.get("max_lat"), "max_lat")
            e, f = _need_num(v.get("min_lon"), "min_lon"), _need_num(v.get("max_lon"), "max_lon")
            if a > b or e > f:
                raise ValidationError("bbox min must be <= max (boxes crossing the antimeridian: use two boxes with OR)")
            return f"lat BETWEEN {d.num(a)} AND {d.num(b)} AND lon BETWEEN {d.num(e)} AND {d.num(f)}"
        raise ValidationError(op)

    def _c_json(self, op, v, c):
        d = self.d
        if not isinstance(v, dict):
            raise ValidationError('json operators need {"path": "a.b", "value": ...}')
        path = v.get("path")
        if not isinstance(path, str) or not re.match(JSON_PATH_RE, path):
            raise ValidationError("invalid json path (letters, digits, underscore, dots)")
        x = v.get("value")
        if op == "json_contains":
            return f"{d.json_is_str('v', path)} AND {d.contains_ci(d.json_str('v', path), _need_str(x))}"
        if isinstance(x, str):
            if op not in ("json_eq", "json_ne"):
                raise ValidationError(f"{op} needs a number")
            sym = "=" if op == "json_eq" else "<>"
            return f"{d.json_is_str('v', path)} AND {d.json_str('v', path)} {sym} {d.str(x)}"
        _need_num(x)
        sym = {"json_eq": "=", "json_ne": "<>", "json_gt": ">", "json_gte": ">=", "json_lt": "<", "json_lte": "<="}[op]
        return f"{d.json_is_num('v', path)} AND {d.json_num('v', path)} {sym} {d.num(x)}"


def describe(n: dict) -> str:
    """Human label for funnel / UI."""
    if "kpi" in n:
        v = n.get("value")
        return f"{n['kpi']} {n['operator']}" + (f" {json.dumps(v)}" if v is not None else "")
    if n["op"] == "NOT":
        return f"NOT ({describe(n['child'])})"
    return "(" + f" {n['op']} ".join(describe(c) for c in n["children"]) + ")"
