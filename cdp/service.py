"""CDPService: the one object the API, CLI and tests talk to.

Responsibilities: compile+validate rules, run counts/members with a
version-stamped cache, single-flight de-duplication, funnel breakdown, saved
segments with staleness detection, KPI lifecycle (retire/purge), freshness.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from .compiler import Compiler, Compiled, describe, naive_utc
from .config import Settings
from .engines import make_engine
from .engines.base import Engine
from .errors import NotFound, ValidationError
from .ingest import Ingestor, IngestResult
from .registry import Registry, rule_kpis


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CDPService:
    def __init__(self, settings: Optional[Settings] = None, engine: Optional[Engine] = None,
                 registry: Optional[Registry] = None):
        self.s = settings or Settings()
        os.makedirs(self.s.data_dir, exist_ok=True)
        self.engine = engine or make_engine(self.s)
        self.engine.init_schema()
        self.reg = registry or Registry(os.path.join(self.s.data_dir, "registry.sqlite"))
        self.ingestor = Ingestor(self.engine, self.reg, self.s.gc_grace_s)
        self._sugg: OrderedDict = OrderedDict()                 # value-suggestion cache (version-stamped keys)
        self._sugg_lock = threading.Lock()
        self._bd: OrderedDict = OrderedDict()                   # breakdown cache (version-stamped keys)
        self._write_lock = threading.Lock()                     # one ingest/purge at a time
        self._flight: dict[str, threading.Lock] = {}
        self._flight_guard = threading.Lock()

    # ------------------------------------------------------------------ keys
    @property
    def compiler(self) -> Compiler:
        """A fresh Compiler per use: it keeps per-compilation state, so instances must not be shared across threads."""
        return Compiler(self.engine.dialect, self.reg, self.s)

    def _compile(self, rule, bu, now) -> Compiled:
        return self.compiler.compile(rule, now, {bu} if bu else None)

    @staticmethod
    def _key(c: Compiled, now: datetime, bu: Optional[str]) -> str:
        """Cache key = canonical rule + (kpi_id, version) of every KPI used + time bucket.

        * canonical rule: children sorted/flattened/deduped, so reordering is a hit
        * kpi versions: a refresh/retire bumps the version -> old keys are simply never asked for again
        * time bucket: relative filters ("last 30 days") only change the answer when 'now' crosses their unit
        """
        versions = sorted((k["kpi_id"], k["version"], k["active_gen"]) for k in c.kpis.values())
        bucket = int(now.replace(tzinfo=timezone.utc).timestamp() // c.rel_unit_s) if c.rel_unit_s else 0
        raw = json.dumps([c.canonical, versions, bucket], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ----------------------------------------------------------------- count
    def _universe(self) -> int:
        return self.engine.query("SELECT count(*) FROM profiles AS p WHERE p.active")[0][0]

    def _run(self, sql: str, c: Compiled) -> list[tuple]:
        """Run a compiled query. The only user-controlled text that can make the engine reject a statement is a
        regex pattern (everything else is validated/escaped), so that case becomes a 422, never a 500."""
        try:
            return self.engine.query(sql, self.s.query_timeout_s)
        except TimeoutError as e:
            raise ValidationError(str(e))
        except Exception as e:
            if c.has_regex:
                raise ValidationError(f"invalid regular expression: {str(e).splitlines()[0][:200]}")
            raise

    def _count_sql(self, c: Compiled) -> int:
        return int(self._run(f"SELECT count(*) FROM profiles AS p WHERE p.active AND ({c.where})", c)[0][0])

    def _cached_count(self, c: Compiled, now: datetime, bu, use_cache: bool) -> tuple[int, bool]:
        key = self._key(c, now, bu)
        if use_cache:
            hit = self.reg.cache_get(key, self.s.cache_ttl_s, time.time())
            if hit:
                return hit[0], True
        with self._flight_guard:
            lock = self._flight.setdefault(key, threading.Lock())
        with lock:                                              # single-flight: identical concurrent queries compute once
            if use_cache:
                hit = self.reg.cache_get(key, self.s.cache_ttl_s, time.time())
                if hit:
                    return hit[0], True
            n = self._count_sql(c)
            self.reg.cache_put(key, n, None, time.time(), self.s.cache_max_entries)
        with self._flight_guard:
            self._flight.pop(key, None)
        return n, False

    def count(self, rule: dict, *, bu: Optional[str] = None, now: Optional[datetime] = None,
              explain: bool = False, use_cache: bool = True) -> dict:
        t0 = time.time()
        now = naive_utc(now) if now else _utcnow()
        c = self._compile(rule, bu, now)
        n, cached = self._cached_count(c, now, bu, use_cache)
        out = {"count": n, "universe": self._universe(), "cached": cached, "conditions": c.leaves,
               "kpis_used": sorted(c.kpis), **self._freshness_info(c, now)}
        if explain and c.canonical is not None:
            out["funnel"] = self._funnel(rule, bu, now, use_cache)
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        return out

    def _funnel(self, raw_rule: dict, bu, now, use_cache) -> list[dict]:
        """Cumulative audience after each top-level condition, in the order the USER built them.

        Each prefix is an ordinary cached count, so editing the last condition only recomputes one step.
        """
        raw = raw_rule.get("children") if str(raw_rule.get("op", "AND")).upper() == "AND" and "kpi" not in raw_rule else [raw_rule]
        kids = [(k, self.compiler.normalize(k)) for k in (raw or [])]
        kids = [(k, nk) for k, nk in kids if nk is not None]
        rows, prev = [], None
        for i in range(1, len(kids) + 1):
            sub = [k for k, _ in kids[:i]]
            rule = sub[0] if len(sub) == 1 else {"op": "AND", "children": sub}
            n, cached = self._cached_count(self._compile(rule, bu, now), now, bu, use_cache)
            rows.append({"step": i, "condition": describe(kids[i - 1][1]), "count": n,
                         "removed": None if prev is None else prev - n, "cached": cached})
            prev = n
        return rows

    def members(self, rule: dict, *, bu: Optional[str] = None, limit: int = 50, offset: int = 0,
                now: Optional[datetime] = None) -> dict:
        if not (0 < limit <= 100_000) or offset < 0:
            raise ValidationError("limit must be 1..100000 and offset >= 0")
        now = naive_utc(now) if now else _utcnow()
        c = self._compile(rule, bu, now)
        rows = self._run(
            f"SELECT p.customer_id FROM profiles AS p WHERE p.active AND ({c.where}) "
            f"ORDER BY p.profile_idx LIMIT {int(limit)} OFFSET {int(offset)}", c)
        return {"customer_ids": [r[0] for r in rows], "limit": limit, "offset": offset}

    def profile(self, customer_id: str, *, bu: Optional[str] = None) -> dict:
        """All current KPI values of one customer (for the 'inspect profile' drawer)."""
        d = self.engine.dialect
        r = self.engine.query(f"SELECT profile_idx FROM profiles AS p WHERE customer_id = {d.str(customer_id)}")
        if not r:
            raise NotFound(f"unknown customer '{customer_id}'")
        pid = int(r[0][0])
        out = {}
        from .types import TABLE_FOR
        for k in self.reg.list_kpis(bu=bu):
            if not k["active_gen"]:
                continue
            t = TABLE_FOR[k["type"]]
            cols = "lat, lon" if k["type"] == "geo" else "v"
            row = self.engine.query(
                f"SELECT {cols} FROM {d.tbl(t)} WHERE kpi_id={k['kpi_id']} AND gen={k['active_gen']} AND profile_idx={pid} LIMIT 1")
            if row:
                v = row[0]
                if k["type"] == "geo":
                    out[k["name"]] = {"lat": float(v[0]), "lon": float(v[1])}
                elif k["type"] == "json":
                    out[k["name"]] = json.loads(v[0])
                elif k["type"] == "number":
                    out[k["name"]] = float(v[0])              # same JSON type whichever engine answered
                elif k["type"] == "datetime":
                    out[k["name"]] = v[0].isoformat() if hasattr(v[0], "isoformat") else str(v[0]).replace(" ", "T")
                else:
                    out[k["name"]] = v[0]
        return {"customer_id": customer_id, "kpis": out}

    # ------------------------------------------------------------- freshness
    def _freshness_info(self, c: Compiled, now: datetime) -> dict:
        loaded = [k["loaded_at"] for k in c.kpis.values() if k["loaded_at"]]
        warnings = []
        for name, k in c.kpis.items():
            if k["max_age_hours"] is not None and k["loaded_at"]:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(k["loaded_at"].replace("Z", "+00:00"))
                       ).total_seconds() / 3600
                if age > k["max_age_hours"]:
                    warnings.append(f"{name} is {age:.1f}h old (SLA {k['max_age_hours']}h)")
        return {"data_as_of": min(loaded) if loaded else None, "warnings": warnings}

    def freshness_report(self) -> list[dict]:
        return self.reg.freshness()

    # -------------------------------------------------------------- ingest
    def ingest(self, source: str, **kw) -> IngestResult:
        with self._write_lock:
            self._gc_locked()                       # opportunistic: collect generations whose grace period is over
            self.ingestor.gc_grace_s = self.s.gc_grace_s
            return self.ingestor.ingest(source, **kw)

    def _gc_locked(self) -> int:
        due = self.reg.due_gc(time.time())
        by_type: dict[str, list[tuple[int, int]]] = {}
        for d in due:
            by_type.setdefault(d["type"], []).append((d["kpi_id"], d["gen"]))
        for t, pairs in by_type.items():
            self.engine.drop_generations(t, pairs)
        self.reg.done_gc(due)
        return len(due)

    def gc(self) -> dict:
        """Delete superseded KPI generations whose grace period has passed (also runs before every ingest)."""
        with self._write_lock:
            return {"generations_deleted": self._gc_locked(), "pending": self.reg.pending_gc_count()}

    def ingest_records(self, records: list[dict], **kw) -> IngestResult:
        """JSON records -> NDJSON temp file -> same pipeline (so JSON API == file ingestion semantics)."""
        if not records:
            raise ValidationError("no records")
        fd, path = tempfile.mkstemp(suffix=".ndjson", prefix="cdp_json_")
        try:
            with os.fdopen(fd, "w") as f:
                for r in records:
                    f.write(json.dumps(r, default=str) + "\n")
            return self.ingest(path, **kw)
        finally:
            os.unlink(path)

    # ------------------------------------------------------------ lifecycle
    def retire_kpi(self, name: str) -> dict:
        with self._write_lock:
            affected = [s for s in self.reg.segments_using([name])]
            k = self.reg.retire(name)
        return {"kpi": k, "segments_broken": [{"id": s["id"], "name": s["name"]} for s in affected]}

    def retire_model(self, model: str) -> dict:
        with self._write_lock:
            ks = self.reg.retire_by_model(model)
            names = [k["name"] for k in ks]
            affected = self.reg.segments_using(names)
        return {"retired": names, "segments_broken": [{"id": s["id"], "name": s["name"]} for s in affected]}

    def purge(self) -> dict:
        """Physically delete data of retired KPIs (the expensive part is separated from the instant part)."""
        with self._write_lock:
            todo = self.reg.retired_unpurged()
            by_type: dict[str, list[int]] = {}
            for k in todo:
                by_type.setdefault(k["type"], []).append(k["kpi_id"])
            self.engine.purge(by_type)
            self.reg.mark_purged([k["kpi_id"] for k in todo])
        return {"purged_kpis": len(todo)}

    SUGGEST_MAX = 50
    BD_BINS = 8

    def _bd_cached(self, key, fn):
        with self._sugg_lock:
            if key in self._bd:
                self._bd.move_to_end(key)
                return self._bd[key]
        val = fn()
        with self._sugg_lock:
            self._bd[key] = val
            while len(self._bd) > 500:
                self._bd.popitem(last=False)
        return val

    def breakdown(self, rule: dict, kpi: str, *, bu: Optional[str] = None, limit: int = 8,
                  now: Optional[datetime] = None) -> dict:
        """Split of one dimension KPI: for every value (or numeric bin), how many profiles are in the audience
        and how many are in the whole base. Powers the 'total vs filtered' charts. text / yes-no / list / number KPIs."""
        from .types import TABLE_FOR
        if not (1 <= limit <= 20):
            raise ValidationError("limit must be 1..20")
        t0 = time.time()
        now = naive_utc(now) if now else _utcnow()
        k = self.reg.get_kpis([kpi]).get(kpi)
        if not k:
            raise NotFound(f"unknown or retired KPI '{kpi}'")
        if bu and k["bu"] not in {bu.upper(), "ALL"}:
            raise ValidationError(f"KPI '{kpi}' belongs to {k['bu']} and is not visible here")
        if k["type"] not in ("string", "bool", "list", "number"):
            raise ValidationError("breakdown supports text, yes/no, list and number KPIs")
        if not k["active_gen"]:
            raise ValidationError(f"KPI '{kpi}' has no data loaded yet")
        c = self._compile(rule, bu, now)
        audience, _ = self._cached_count(c, now, bu, True)
        universe = self._universe()
        d, kt = self.engine.dialect, k["type"]
        t = d.tbl(TABLE_FOR[kt])
        base = f"kpi_id = {int(k['kpi_id'])} AND gen = {int(k['active_gen'])}"
        ver = (k["kpi_id"], k["version"], k["active_gen"])
        aud = f"profile_idx IN (SELECT p.profile_idx FROM profiles AS p WHERE p.active AND ({c.where}))"

        lo = w = None
        bins = self.BD_BINS
        if kt == "number":
            mn, mx = self._bd_cached(("rng",) + ver, lambda: self.engine.query(
                f"SELECT min(v), max(v) FROM {t} WHERE {base}")[0])
            if mn is None:
                mn = mx = 0
            lo, mn, mx = float(mn), float(mn), float(mx)
            bins = 1 if mx == mn else bins
            w = (mx - mn) / bins if mx > mn else 1.0

        def rows(extra: str):
            if kt == "number":
                expr = f"least({d.to_int(f'floor((v - {d.num(lo)}) / {d.num(w)})')}, {bins - 1})"
                sql = f"SELECT b, count(*) FROM (SELECT {expr} AS b FROM {t} WHERE {base}{extra}) AS s GROUP BY b"
            elif kt == "list":
                sql = (f"SELECT e, count(*) FROM (SELECT {d.unnest('v')} AS e FROM {t} WHERE {base}{extra}) AS s "
                       f"WHERE e IS NOT NULL AND e <> '' GROUP BY e")
            else:
                sql = f"SELECT v, count(*) FROM {t} WHERE {base}{extra} GROUP BY v"
            out = {}
            for v, n in self._run(sql, c):
                if kt == "bool":
                    v = "yes" if v in (True, 1, "true", "True") else "no"
                elif kt == "number":
                    v = int(v)
                else:
                    v = str(v)
                out[v] = out.get(v, 0) + int(n)
            return out

        tot = self._bd_cached(("t",) + ver, lambda: rows(""))
        aud_rows = self._bd_cached(("a", self._key(c, now, bu)) + ver, lambda: rows(f" AND {aud}"))

        def lab(i):
            a, b = lo + i * w, lo + (i + 1) * w
            return f"{a:.4g}–{b:.4g}"

        if kt == "number":
            groups = [{"value": lab(i), "audience": aud_rows.get(i, 0), "total": tot.get(i, 0)} for i in range(bins)]
            other = None
        else:
            keys = sorted(set(tot) | set(aud_rows), key=lambda x: (-aud_rows.get(x, 0), -tot.get(x, 0), x))
            groups = [{"value": x, "audience": aud_rows.get(x, 0), "total": tot.get(x, 0)} for x in keys[:limit]]
            rest = keys[limit:]
            other = ({"n": len(rest), "audience": sum(aud_rows.get(x, 0) for x in rest),
                      "total": sum(tot.get(x, 0) for x in rest)} if rest else None)
        missing = None
        if kt != "list":
            missing = {"audience": max(0, audience - sum(aud_rows.values())), "total": max(0, universe - sum(tot.values()))}
        return {"kpi": kpi, "type": kt, "audience": audience, "universe": universe, "groups": groups,
                "other": other, "missing": missing, "elapsed_ms": round((time.time() - t0) * 1000, 1)}

    def suggest_values(self, kpi: str, q: str = "", *, limit: int = 20, bu: Optional[str] = None) -> dict:
        """Type-ahead for string / list KPIs: distinct values matching `q` (case-insensitive, prefix matches
        first, then by frequency). Cached per (kpi, generation, q), so a refresh invalidates it automatically."""
        from .types import TABLE_FOR
        if not (0 < limit <= self.SUGGEST_MAX):
            raise ValidationError(f"limit must be 1..{self.SUGGEST_MAX}")
        q = (q or "").strip()[:100]
        k = self.reg.get_kpis([kpi]).get(kpi)
        if not k:
            raise NotFound(f"unknown or retired KPI '{kpi}'")
        if bu and k["bu"] not in {bu.upper(), "ALL"}:
            raise ValidationError(f"KPI '{kpi}' belongs to {k['bu']} and is not visible here")
        if k["type"] not in ("string", "list"):
            raise ValidationError("value suggestions exist only for string and list KPIs")
        if not k["active_gen"]:
            raise ValidationError(f"KPI '{kpi}' has no data loaded yet")
        key = (k["kpi_id"], k["version"], k["active_gen"], q.lower(), limit)
        with self._sugg_lock:
            if key in self._sugg:
                self._sugg.move_to_end(key)
                return self._sugg[key] | {"cached": True}
        d = self.engine.dialect
        t = d.tbl(TABLE_FOR[k["type"]])
        where = f"kpi_id = {int(k['kpi_id'])} AND gen = {int(k['active_gen'])}"
        inner = (f"SELECT v AS e FROM {t} WHERE {where}" if k["type"] == "string"
                 else f"SELECT {d.unnest('v')} AS e FROM {t} WHERE {where}")
        cond, order = "e IS NOT NULL AND e <> ''", "c DESC, e"
        if q:
            cond += f" AND {d.contains_ci('e', q)}"
            order = f"CASE WHEN {d.starts_ci('e', q)} THEN 0 ELSE 1 END, {order}"
        rows = self.engine.query(f"SELECT e, count(*) AS c FROM ({inner}) AS s WHERE {cond} GROUP BY e "
                                 f"ORDER BY {order} LIMIT {int(limit) + 1}")
        out = {"kpi": kpi, "q": q, "values": [{"value": str(r[0]), "count": int(r[1])} for r in rows[:limit]],
               "truncated": len(rows) > limit, "cached": False}
        with self._sugg_lock:
            self._sugg[key] = out
            while len(self._sugg) > 2000:
                self._sugg.popitem(last=False)
        return out

    def list_kpis(self, bu: Optional[str] = None, **kw) -> list[dict]:
        return [k | {"loaded": bool(k["active_gen"])} for k in self.reg.list_kpis(bu=bu, **kw)]

    # ------------------------------------------------------------- segments
    def save_segment(self, name: str, rule: dict, *, owner: str = "", bu: str = "", seg_id: Optional[int] = None) -> dict:
        if not name.strip():
            raise ValidationError("segment name required")
        now = _utcnow()
        c = self._compile(rule, bu or None, now)        # refuse to save a rule that does not compile
        n, _ = self._cached_count(c, now, bu or None, True)
        vers = {k: v["version"] for k, v in c.kpis.items()}
        s = self.reg.save_segment(name, rule, owner=owner, bu=(bu or "").upper(), seg_id=seg_id,
                                  last_count=n, kpi_versions=vers)
        return self._seg_view(s)

    def _seg_view(self, s: dict) -> dict:
        """Adds `status`: ok | stale (a KPI refreshed since last count) | broken (a KPI was retired)."""
        names = rule_kpis(s["rule"])
        status, why = "ok", []
        for n in names:
            k = self.reg.get_kpi(n, include_retired=True)
            if not k or k["status"] == "retired":
                status = "broken"
                why.append(f"{n} retired")
            elif status != "broken" and s["kpi_versions"].get(n) != k["version"]:
                status = "stale"
                why.append(f"{n} refreshed")
        return {**s, "status": status, "status_reason": why}

    def list_segments(self, bu: Optional[str] = None, owner: Optional[str] = None) -> list[dict]:
        return [self._seg_view(s) for s in self.reg.list_segments(bu, owner)]

    def get_segment(self, seg_id: int) -> dict:
        return self._seg_view(self.reg.get_segment(seg_id))

    def delete_segment(self, seg_id: int) -> None:
        self.reg.delete_segment(seg_id)

    def refresh_segment(self, seg_id: int) -> dict:
        s = self.reg.get_segment(seg_id)
        now = _utcnow()
        c = self._compile(s["rule"], s["bu"] or None, now)
        n, _ = self._cached_count(c, now, s["bu"] or None, True)
        self.reg.record_segment_count(seg_id, n, {k: v["version"] for k, v in c.kpis.items()})
        return self.get_segment(seg_id)

    def refresh_stale_segments(self) -> list[dict]:
        out = []
        for s in self.list_segments():
            if s["status"] == "stale":
                out.append(self.refresh_segment(s["id"]))
        return out

    # ----------------------------------------------------------------- misc
    def stats(self) -> dict:
        return {"engine": self.engine.name, **self.engine.stats(), "cache": self.reg.cache_stats(),
                "kpis_active": len(self.reg.list_kpis()), "kpis_retired": len(self.reg.list_kpis(include_retired=True)) - len(self.reg.list_kpis())}

    def close(self):
        self.engine.close()
