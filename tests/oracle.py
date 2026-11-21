"""Independent reference implementation of the rule semantics, in plain Python over the
generated wide Parquet. Engine results must equal the oracle for every rule."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

import duckdb

REL = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}


def load_rows(path: str) -> list[dict]:
    con = duckdb.connect()
    cur = con.execute(f"SELECT * FROM read_parquet('{path}')")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def hav(lat1, lon1, lat2, lon2):
    a = (math.sin(math.radians(lat1 - lat2) / 2) ** 2 +
         math.cos(math.radians(lat2)) * math.cos(math.radians(lat1)) * math.sin(math.radians(lon1 - lon2) / 2) ** 2)
    return 12742.0 * math.asin(math.sqrt(min(1.0, a)))


def _dt(v):
    s = v.replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=None), bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v))


def _jget(obj, path):
    for p in path.split("."):
        if not isinstance(obj, dict) or p not in obj or obj[p] is None:
            return None, False
        obj = obj[p]
    return obj, True


def leaf(row: dict, n: dict, now: datetime) -> bool:
    v, op, x = row.get(n["kpi"]), n["operator"], n.get("value")
    if op == "is_null":
        return v is None
    if op == "is_not_null":
        return v is not None
    if v is None:
        return False
    # number
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if op == "eq": return v == x
        if op == "ne": return v != x
        if op == "gt": return v > x
        if op == "gte": return v >= x
        if op == "lt": return v < x
        if op == "lte": return v <= x
        if op == "between": return x[0] <= v <= x[1]
        if op == "in": return v in x
        if op == "not_in": return v not in x
    if isinstance(v, bool):
        return v if op == "is_true" else (not v)
    if isinstance(v, str):
        lo = v.lower()
        if op == "eq": return v == x
        if op == "ne": return v != x
        if op == "in": return v in x
        if op == "not_in": return v not in x
        if op == "contains": return x.lower() in lo
        if op == "not_contains": return x.lower() not in lo
        if op == "starts_with": return lo.startswith(x.lower())
        if op == "ends_with": return lo.endswith(x.lower())
        if op == "regex": return re.search(x, v) is not None
    if isinstance(v, datetime):
        if op == "on_date":
            d, _ = _dt(x)
            d0 = d.replace(hour=0, minute=0, second=0, microsecond=0)
            return d0 <= v < d0 + timedelta(days=1)
        if op == "before": return v < _dt(x)[0]
        if op == "after": return v >= _dt(x)[0]
        if op == "between":
            a, (b, b_date) = _dt(x[0])[0], _dt(x[1])
            return (a <= v < b + timedelta(days=1)) if b_date else (a <= v <= b)
        if op in ("within_last", "not_within_last"):
            sec = REL[x.get("unit", "day")]
            epoch = (now - datetime(1970, 1, 1)).total_seconds()
            cut = datetime(1970, 1, 1) + timedelta(seconds=math.floor(epoch / sec) * sec - x["n"] * sec)
            return v >= cut if op == "within_last" else v < cut
    if isinstance(v, list):
        if op == "contains": return x in v
        if op == "not_contains": return x not in v
        if op == "contains_any": return any(i in v for i in x)
        if op == "contains_all": return all(i in v for i in x)
        if op == "size_eq": return len(v) == x
        if op == "size_gt": return len(v) > x
        if op == "size_gte": return len(v) >= x
        if op == "size_lt": return len(v) < x
        if op == "is_empty": return len(v) == 0
        if op == "first_is": return len(v) > 0 and v[0] == x
    if isinstance(v, dict):
        if set(v) == {"lat", "lon"}:
            if op == "within_radius": return hav(v["lat"], v["lon"], x["lat"], x["lon"]) <= x["km"]
            if op == "outside_radius": return hav(v["lat"], v["lon"], x["lat"], x["lon"]) > x["km"]
            if op == "within_bbox":
                return x["min_lat"] <= v["lat"] <= x["max_lat"] and x["min_lon"] <= v["lon"] <= x["max_lon"]
        else:
            val, ok = _jget(v, x["path"])
            if not ok:
                return False
            t = x.get("value")
            if op == "json_contains":
                return isinstance(val, str) and t.lower() in val.lower()
            is_num = isinstance(val, (int, float)) and not isinstance(val, bool)
            if isinstance(t, str):
                if not isinstance(val, str):
                    return False
                return (val == t) if op == "json_eq" else (val != t)
            if not is_num:
                return False
            return {"json_eq": val == t, "json_ne": val != t, "json_gt": val > t, "json_gte": val >= t,
                    "json_lt": val < t, "json_lte": val <= t}[op]
    raise AssertionError(f"oracle cannot evaluate {n}")


def evaluate(row: dict, node: dict, now: datetime) -> bool:
    if "kpi" in node:
        return leaf(row, node, now)
    op = node.get("op", "AND").upper()
    if op == "NOT":
        return not evaluate(row, node["child"], now)
    kids = [c for c in node.get("children", [])]
    if not kids:
        return True
    return all(evaluate(row, c, now) for c in kids) if op == "AND" else any(evaluate(row, c, now) for c in kids)


def count(rows: list[dict], rule: dict, now: datetime) -> int:
    return sum(1 for r in rows if evaluate(r, rule, now))
