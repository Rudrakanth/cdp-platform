"""Ingestion: files (Parquet / CSV / JSON-lines, local or s3://) -> KPIs.

Contract: a *wide* file with one id column (default `customer_id`) and any
number of KPI columns. Each column is one KPI. Types are inferred from the file
(overridable), new KPIs are auto-registered, and a column is dropped for a
profile when its value is NULL (absence == null; nothing is stored for it).

Modes
  replace  full refresh of each KPI listed in the file. Writes a NEW generation
           beside the live one, then atomically flips the pointer. Profiles
           missing from the file lose the value. Right for nightly ML scoring.
  upsert   delta: only the profiles in the file change; everything else keeps
           its value. Right for streaming/micro-batch corrections.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import duckdb

from .engines.base import Engine
from .errors import Conflict, ValidationError
from .registry import Registry
from .types import KPI_NAME_RE

NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
           "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL", "INT")


def infer_type(duck_type: str) -> str:
    t = duck_type.upper().strip()
    if t.endswith("[]"):
        inner = t[:-2]
        return "json" if inner.startswith(("STRUCT", "MAP")) or inner.endswith("]") else "list"
    if t.startswith("STRUCT"):
        inner = t[t.index("(") + 1:-1]
        if "(" not in inner:
            names = [p.strip().split()[0].strip('"').lower() for p in inner.split(",")]
            if sorted(names) == ["lat", "lon"]:
                return "geo"
        return "json"
    if t.startswith(("MAP", "UNION")) or t == "JSON":
        return "json"
    if t in ("VARCHAR", "STRING", "TEXT") or t.startswith("VARCHAR"):
        return "string"
    if t == "BOOLEAN":
        return "bool"
    if t.startswith(("DATE", "TIMESTAMP")):
        return "datetime"
    if t.split("(")[0] in NUMERIC:
        return "number"
    raise ValidationError(f"unsupported source column type {duck_type}")


def _normalised(col: str, kpi_type: str, src_type: str) -> tuple[str, str]:
    """(select list, WHERE predicate) producing the canonical staged value columns."""
    c = '"' + col + '"'
    if kpi_type == "number":
        return f"CAST({c} AS DOUBLE) AS v", f"{c} IS NOT NULL AND isfinite(CAST({c} AS DOUBLE))"
    if kpi_type == "string":
        return f"CAST({c} AS VARCHAR) AS v", f"{c} IS NOT NULL"
    if kpi_type == "bool":
        return f"CAST({c} AS BOOLEAN) AS v", f"{c} IS NOT NULL"
    if kpi_type == "datetime":
        return f"CAST({c} AS TIMESTAMP) AS v", f"{c} IS NOT NULL"
    if kpi_type == "list":
        return (f"list_filter(CAST({c} AS VARCHAR[]), lambda x: x IS NOT NULL) AS v", f"{c} IS NOT NULL")
    if kpi_type == "geo":
        return (f"CAST({c}.lat AS DOUBLE) AS lat, CAST({c}.lon AS DOUBLE) AS lon",
                f"{c} IS NOT NULL AND {c}.lat BETWEEN -90 AND 90 AND {c}.lon BETWEEN -180 AND 180")
    if kpi_type == "json":
        if src_type.upper() in ("VARCHAR", "JSON"):
            return f"CAST({c} AS VARCHAR) AS v", f"{c} IS NOT NULL AND json_valid(CAST({c} AS VARCHAR))"
        return f"CAST(to_json({c}) AS VARCHAR) AS v", f"{c} IS NOT NULL"
    raise ValidationError(kpi_type)


BATCH = 100      # KPI columns per staged file / per engine INSERT


@dataclass
class IngestResult:
    mode: str
    model: str
    kpis: list = field(default_factory=list)       # [{name,type,rows,gen,version,new}]
    profiles_added: int = 0
    seconds: float = 0.0

    @property
    def rows(self) -> int:
        return sum(k["rows"] for k in self.kpis)


class Ingestor:
    def __init__(self, engine: Engine, registry: Registry, gc_grace_s: float = 120.0):
        self.engine, self.reg, self.gc_grace_s = engine, registry, gc_grace_s

    @staticmethod
    def _open(path: str) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        if path.startswith(("s3://", "http://", "https://")):
            con.execute("INSTALL httpfs; LOAD httpfs;")
        low = path.lower().split("?")[0]
        q = "'" + path.replace("'", "''") + "'"
        if low.endswith(".parquet"):
            rel = f"read_parquet({q})"
        elif low.endswith((".csv", ".csv.gz", ".tsv")):
            rel = f"read_csv({q}, header=true, auto_detect=true, sample_size=100000)"
        elif low.endswith((".jsonl", ".ndjson", ".json")):
            rel = f"read_json({q}, format='auto', sample_size=100000, maximum_object_size=104857600)"
        else:
            raise ValidationError("source must be .parquet, .csv, .tsv, .jsonl, .ndjson or .json")
        try:
            con.execute(f"CREATE VIEW src AS SELECT * FROM {rel}")
            con.execute("SELECT * FROM src LIMIT 0")        # views are lazy: force the file to be opened/parsed now
        except duckdb.Error as e:
            con.close()
            raise ValidationError(f"cannot read source file: {str(e).splitlines()[0][:200]}") from None
        return con

    def ingest(self, source: str, *, model: str, mode: str = "replace", meta: Optional[dict] = None,
               id_column: str = "customer_id", add_profiles: bool = True, only: Optional[list] = None,
               types: Optional[dict] = None, bu: str = "ALL", category: str = "uncategorized",
               max_age_hours: Optional[float] = None, vacuum: bool = True) -> IngestResult:
        if mode not in ("replace", "upsert"):
            raise ValidationError("mode must be 'replace' or 'upsert'")
        if not model:
            raise ValidationError("model (source system / ML model name) is required; it is what lets you retire KPIs in bulk")
        t0 = time.time()
        meta, types = meta or {}, types or {}
        con = self._open(source)
        stage = tempfile.mkdtemp(prefix="cdp_stage_")
        res = IngestResult(mode=mode, model=model)
        try:
            cols = {r[0]: r[1] for r in con.execute("DESCRIBE src").fetchall()}
            if id_column not in cols:
                raise ValidationError(f"id column '{id_column}' not found; columns: {', '.join(cols)}")
            n, nd, nn = con.execute(
                f'SELECT count(*), count(DISTINCT "{id_column}"), count("{id_column}") FROM src').fetchone()
            if nn != n:
                raise ValidationError(f"{n - nn} rows have a NULL {id_column}")
            if nd != n:
                raise ValidationError(f"{n - nd} duplicate {id_column} values in source; one row per profile expected")

            # ---- validate everything before touching any state
            plan = []
            for name, st in cols.items():
                if name == id_column or (only and name not in only):
                    continue
                if not re.match(KPI_NAME_RE, name):
                    raise ValidationError(f"column '{name}' is not a valid KPI name (lowercase snake_case)")
                kt = types.get(name) or infer_type(st)
                existing = self.reg.get_kpi(name, include_retired=True)
                if existing and existing["status"] == "retired":
                    raise Conflict(f"KPI '{name}' is retired; ids are never reused, rename the column")
                if existing and existing["type"] != kt:
                    raise Conflict(f"KPI '{name}' is {existing['type']} in the registry but {kt} in the file")
                plan.append((name, st, kt, existing is None))
            if not plan:
                raise ValidationError("no KPI columns found in source")

            # ---- profiles
            cust = os.path.join(stage, "customers.parquet")
            con.execute(f"COPY (SELECT CAST(\"{id_column}\" AS VARCHAR) AS customer_id FROM src) TO '{cust}' (FORMAT parquet)")
            if add_profiles:
                res.profiles_added = self.engine.add_profiles(cust)

            # ---- register KPIs and decide each one's target generation
            items = []          # dicts: name, type, src_type, kpi, gen, upsert
            for name, st, kt, is_new in plan:
                m = meta.get(name, {})
                k = self.reg.register_kpi(
                    name, kt, description=m.get("description", ""), category=m.get("category", category),
                    bu=m.get("bu", bu), source_model=model, max_age_hours=m.get("max_age_hours", max_age_hours))
                if not is_new and m:
                    k = self.reg.update_meta(name, **{x: y for x, y in m.items() if x != "type"}, source_model=model)
                upsert = mode == "upsert" and k["active_gen"] != 0
                gen = k["active_gen"] if upsert else self.reg.allocate_gen(k["kpi_id"])
                items.append({"name": name, "type": kt, "src": st, "kpi": k, "gen": gen, "upsert": upsert, "new": is_new})

            by_type: dict[str, list[dict]] = {}
            for it in items:
                by_type.setdefault(it["type"], []).append(it)

            # ---- LOAD phase: write rows under their target generation. Nothing is visible yet for replace;
            #      for upsert the delete+insert is one engine transaction where the engine supports it.
            ver = int(time.time() * 1000)
            try:
                staged: list[tuple[str, str, bool]] = []     # (type, path, has_upsert)
                for kt, group in by_type.items():
                    for bi in range(0, len(group), BATCH):
                        chunk = group[bi:bi + BATCH]
                        branches = []
                        for it in chunk:
                            sel, where = _normalised(it["name"], kt, it["src"])
                            branches.append(
                                f"SELECT CAST(\"{id_column}\" AS VARCHAR) AS customer_id, CAST({it['kpi']['kpi_id']} AS INTEGER) AS kpi_id, "
                                f"CAST({it['gen']} AS INTEGER) AS gen, {sel} FROM src WHERE {where}")
                        path = os.path.join(stage, f"{kt}_{bi // BATCH}.parquet")
                        con.execute(f"COPY ({' UNION ALL '.join(branches)}) TO '{path}' (FORMAT parquet)")
                        staged.append((kt, path, any(i["upsert"] for i in chunk)))
                with self.engine.atomic():
                    for kt, path, has_upsert in staged:
                        if has_upsert:
                            self.engine.delete_for_upsert(kt, path)
                        self.engine.load_long(kt, path, ver)
                counts: dict[int, int] = {}
                for kt, group in by_type.items():
                    counts.update(self.engine.count_rows_many(kt, [(i["kpi"]["kpi_id"], i["gen"]) for i in group]))
            except Exception:
                # nothing was published: live generations are untouched; drop the orphan rows (best effort)
                for kt, group in by_type.items():
                    try:
                        self.engine.vacuum_many(kt, {i["kpi"]["kpi_id"]: self.reg.require_kpi(i["name"])["active_gen"] for i in group})
                    except Exception:
                        pass
                raise

            # ---- PUBLISH phase: one SQLite transaction flips every KPI of this file together
            published = self.reg.publish_many([(i["kpi"]["kpi_id"], i["gen"], counts[i["kpi"]["kpi_id"]]) for i in items])
            for it, k in zip(items, published):
                res.kpis.append({"name": it["name"], "type": it["type"], "rows": counts[it["kpi"]["kpi_id"]],
                                 "gen": it["gen"], "version": k["version"], "new": it["new"]})
            if vacuum:                      # superseded generations are deleted later (grace period), never immediately
                due = time.time() + self.gc_grace_s
                for it in items:
                    prev = it["kpi"]["active_gen"]
                    if not it["upsert"] and prev:
                        self.reg.schedule_gc(it["kpi"]["kpi_id"], it["type"], prev, due)
        finally:
            con.close()
            shutil.rmtree(stage, ignore_errors=True)
        res.seconds = round(time.time() - t0, 3)
        self.reg.log_ingest(source=source, model=model, mode=mode, kpis=[k["name"] for k in res.kpis],
                            profiles_added=res.profiles_added, rows=res.rows, seconds=res.seconds)
        return res
