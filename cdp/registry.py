"""Control plane: KPI registry, saved segments, count cache.

Small, transactional metadata lives in SQLite (Postgres in a real SaaS
deployment). The analytical data plane (engines/) never stores metadata.

Key invariants
- kpi_id is AUTOINCREMENT: a retired id is never reused, so stale rows of an
  old KPI can never be read as a new KPI's data.
- A KPI's data is visible only through (kpi_id, active_gen). Loading writes a
  *new* generation, then flips active_gen in one statement (atomic publish).
- `version` increases on every publish and is part of every cache key, so a
  refresh invalidates exactly the cached queries that used that KPI.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .errors import Conflict, NotFound, ValidationError
from .types import KPI_NAME_RE, KPI_TYPES

SCHEMA = """
CREATE TABLE IF NOT EXISTS kpis(
  kpi_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  description TEXT DEFAULT '',
  category TEXT DEFAULT 'uncategorized',
  bu TEXT DEFAULT 'ALL',
  source_model TEXT DEFAULT '',
  status TEXT DEFAULT 'active',
  active_gen INTEGER DEFAULT 0,
  next_gen INTEGER DEFAULT 1,
  version INTEGER DEFAULT 0,
  row_count INTEGER DEFAULT 0,
  max_age_hours REAL DEFAULT NULL,
  loaded_at TEXT,
  created_at TEXT,
  retired_at TEXT,
  purged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_kpis_model ON kpis(source_model);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  owner TEXT DEFAULT '',
  bu TEXT DEFAULT '',
  rule_json TEXT NOT NULL,
  created_at TEXT, updated_at TEXT,
  last_count INTEGER, last_count_at TEXT,
  kpi_versions TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS count_cache(
  key TEXT PRIMARY KEY,
  count INTEGER NOT NULL,
  universe INTEGER,
  created_at REAL NOT NULL,
  hits INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_gc(
  kpi_id INTEGER NOT NULL, type TEXT NOT NULL, gen INTEGER NOT NULL, delete_after REAL NOT NULL,
  PRIMARY KEY(kpi_id, gen)
);
CREATE TABLE IF NOT EXISTS ingest_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, source TEXT, model TEXT, mode TEXT, kpis TEXT, profiles_added INTEGER, rows INTEGER, seconds REAL
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Registry:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    # One SQLite connection is shared by all request threads, and a connection must never run two
    # statements at once, so EVERY statement (reads included) goes through these two helpers.
    def _one(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ KPIs
    @staticmethod
    def _kpi(row: sqlite3.Row | None) -> Optional[dict]:
        return dict(row) if row else None

    def register_kpi(self, name: str, type: str, *, description: str = "", category: str = "uncategorized",
                     bu: str = "ALL", source_model: str = "", max_age_hours: float | None = None) -> dict:
        if not re.match(KPI_NAME_RE, name):
            raise ValidationError(f"invalid KPI name '{name}' (lowercase snake_case, max 64 chars)")
        if type not in KPI_TYPES:
            raise ValidationError(f"invalid KPI type '{type}'")
        with self.lock:
            cur = self.get_kpi(name, include_retired=True)
            if cur:
                if cur["status"] == "retired":
                    raise Conflict(f"KPI '{name}' is retired; pick a new name (ids are never reused)")
                if cur["type"] != type:
                    raise Conflict(f"KPI '{name}' is {cur['type']}, source column is {type}")
                return cur
            self.db.execute(
                "INSERT INTO kpis(name,type,description,category,bu,source_model,max_age_hours,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (name, type, description, category, (bu or "ALL").upper(), source_model, max_age_hours, utcnow_iso()))
            return self.get_kpi(name)

    def update_meta(self, name: str, **fields) -> dict:
        allowed = {"description", "category", "bu", "source_model", "max_age_hours"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "bu" in sets:
            sets["bu"] = str(sets["bu"]).upper()
        if sets:
            with self.lock:
                self.db.execute(f"UPDATE kpis SET {','.join(k + '=?' for k in sets)} WHERE name=?",
                                (*sets.values(), name))
        return self.require_kpi(name, include_retired=True)

    def get_kpi(self, name: str, include_retired: bool = False) -> Optional[dict]:
        row = self._one("SELECT * FROM kpis WHERE name=?", (name,))
        k = self._kpi(row)
        if k and k["status"] == "retired" and not include_retired:
            return None
        return k

    def require_kpi(self, name: str, include_retired: bool = False) -> dict:
        k = self.get_kpi(name, include_retired)
        if not k:
            raise NotFound(f"unknown or retired KPI '{name}'")
        return k

    def list_kpis(self, *, bu: str | None = None, include_retired: bool = False, category: str | None = None,
                  model: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM kpis WHERE 1=1", []
        if not include_retired:
            q += " AND status='active'"
        if bu:
            q += " AND bu IN ('ALL', ?)"
            args.append(bu.upper())
        if category:
            q += " AND category=?"
            args.append(category)
        if model:
            q += " AND source_model=?"
            args.append(model)
        return [dict(r) for r in self._all(q + " ORDER BY category, name", args)]

    def allocate_gen(self, kpi_id: int) -> int:
        """Reserve a fresh generation number (never reused, even if a load fails)."""
        with self.lock:
            g = self._one("SELECT next_gen FROM kpis WHERE kpi_id=?", (kpi_id,))[0]
            self.db.execute("UPDATE kpis SET next_gen=? WHERE kpi_id=?", (g + 1, kpi_id))
            return g

    def publish(self, kpi_id: int, gen: int, row_count: int) -> dict:
        """Atomic flip: from this statement on, readers see `gen`."""
        with self.lock:
            self.db.execute(
                "UPDATE kpis SET active_gen=?, version=version+1, row_count=?, loaded_at=? WHERE kpi_id=? AND status='active'",
                (gen, row_count, utcnow_iso(), kpi_id))
            return dict(self._one("SELECT * FROM kpis WHERE kpi_id=?", (kpi_id,)))

    def publish_many(self, items: list[tuple[int, int, int]]) -> list[dict]:
        """Flip many KPIs in ONE transaction: readers see the old or the new generation of the whole
        batch, never a mix (this is what keeps KPIs from the same ML model mutually consistent)."""
        now = utcnow_iso()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for kpi_id, gen, rows in items:
                    self.db.execute("UPDATE kpis SET active_gen=?, version=version+1, row_count=?, loaded_at=? "
                                    "WHERE kpi_id=? AND status='active'", (gen, rows, now, kpi_id))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            return [dict(self._one("SELECT * FROM kpis WHERE kpi_id=?", (i,))) for i, _, _ in items]

    def schedule_gc(self, kpi_id: int, kpi_type: str, gen: int, delete_after: float) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pending_gc(kpi_id,type,gen,delete_after) VALUES(?,?,?,?)",
                            (kpi_id, kpi_type, gen, delete_after))

    def due_gc(self, now_ts: float) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM pending_gc WHERE delete_after <= ?", (now_ts,))]

    def done_gc(self, items: list[dict]) -> None:
        with self.lock:
            for i in items:
                self.db.execute("DELETE FROM pending_gc WHERE kpi_id=? AND gen=?", (i["kpi_id"], i["gen"]))

    def pending_gc_count(self) -> int:
        return self._one("SELECT count(*) FROM pending_gc")[0]

    def get_kpis(self, names, include_retired: bool = False) -> dict[str, dict]:
        """Consistent snapshot of several KPIs in ONE statement (publish_many commits under the same lock),
        so a query never mixes the old generation of one KPI with the new generation of another."""
        names = list(dict.fromkeys(names))
        if not names:
            return {}
        rows = self._all(f"SELECT * FROM kpis WHERE name IN ({','.join('?' * len(names))})", names)
        out = {r["name"]: dict(r) for r in rows}
        return out if include_retired else {n: k for n, k in out.items() if k["status"] != "retired"}

    def retire(self, name: str) -> dict:
        with self.lock:
            k = self.require_kpi(name)
            self.db.execute("UPDATE kpis SET status='retired', retired_at=?, version=version+1 WHERE kpi_id=?",
                            (utcnow_iso(), k["kpi_id"]))
            return self.require_kpi(name, include_retired=True)

    def retire_by_model(self, model: str) -> list[dict]:
        with self.lock:
            names = [r["name"] for r in self._all(
                "SELECT name FROM kpis WHERE source_model=? AND status='active'", (model,))]
            now = utcnow_iso()
            self.db.execute(
                "UPDATE kpis SET status='retired', retired_at=?, version=version+1 WHERE source_model=? AND status='active'",
                (now, model))
            return [self.require_kpi(n, include_retired=True) for n in names]

    def retired_unpurged(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM kpis WHERE status='retired' AND purged=0")]

    def mark_purged(self, ids: Iterable[int]) -> None:
        with self.lock:
            for i in ids:
                self.db.execute("UPDATE kpis SET purged=1 WHERE kpi_id=?", (i,))

    def freshness(self, now: datetime | None = None) -> list[dict]:
        """KPIs whose data is older than their own max_age_hours SLA."""
        now = now or datetime.now(timezone.utc)
        out = []
        for k in self.list_kpis():
            if k["max_age_hours"] is None:
                continue
            if not k["loaded_at"]:
                out.append({**k, "age_hours": None, "reason": "never loaded"})
                continue
            age = (now - datetime.fromisoformat(k["loaded_at"].replace("Z", "+00:00"))).total_seconds() / 3600
            if age > k["max_age_hours"]:
                out.append({**k, "age_hours": round(age, 2), "reason": "older than SLA"})
        return out

    # -------------------------------------------------------------- segments
    def save_segment(self, name: str, rule: dict, *, owner: str = "", bu: str = "", seg_id: int | None = None,
                     last_count: int | None = None, kpi_versions: dict | None = None) -> dict:
        now = utcnow_iso()
        with self.lock:
            if seg_id is None:
                cur = self.db.execute(
                    "INSERT INTO segments(name,owner,bu,rule_json,created_at,updated_at,last_count,last_count_at,kpi_versions)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (name, owner, bu, json.dumps(rule), now, now, last_count,
                     now if last_count is not None else None, json.dumps(kpi_versions or {})))
                seg_id = cur.lastrowid
            else:
                self.get_segment(seg_id)
                self.db.execute(
                    "UPDATE segments SET name=?, owner=?, bu=?, rule_json=?, updated_at=?, last_count=?, last_count_at=?, kpi_versions=? WHERE id=?",
                    (name, owner, bu, json.dumps(rule), now, last_count,
                     now if last_count is not None else None, json.dumps(kpi_versions or {}), seg_id))
            return self.get_segment(seg_id)

    def record_segment_count(self, seg_id: int, count: int, kpi_versions: dict) -> None:
        with self.lock:
            self.db.execute("UPDATE segments SET last_count=?, last_count_at=?, kpi_versions=? WHERE id=?",
                            (count, utcnow_iso(), json.dumps(kpi_versions), seg_id))

    def get_segment(self, seg_id: int) -> dict:
        row = self._one("SELECT * FROM segments WHERE id=?", (seg_id,))
        if not row:
            raise NotFound(f"segment {seg_id} not found")
        d = dict(row)
        d["rule"] = json.loads(d.pop("rule_json"))
        d["kpi_versions"] = json.loads(d["kpi_versions"] or "{}")
        return d

    def list_segments(self, bu: str | None = None, owner: str | None = None) -> list[dict]:
        q, args = "SELECT id FROM segments WHERE 1=1", []
        if bu:
            q += " AND (bu=? OR bu='')"
            args.append(bu.upper())
        if owner:
            q += " AND owner=?"
            args.append(owner)
        return [self.get_segment(r["id"]) for r in self._all(q + " ORDER BY updated_at DESC", args)]

    def delete_segment(self, seg_id: int) -> None:
        with self.lock:
            self.get_segment(seg_id)
            self.db.execute("DELETE FROM segments WHERE id=?", (seg_id,))

    def segments_using(self, kpi_names: Iterable[str]) -> list[dict]:
        names = set(kpi_names)
        out = []
        for s in self.list_segments():
            if names & _rule_kpis(s["rule"]):
                out.append(s)
        return out

    # ----------------------------------------------------------------- cache
    def cache_get(self, key: str, ttl_s: int, now_ts: float) -> Optional[tuple[int, int | None]]:
        row = self._one("SELECT count, universe, created_at FROM count_cache WHERE key=?", (key,))
        if not row:
            return None
        if now_ts - row["created_at"] > ttl_s:
            return None  # TTL is only a safety net; version-stamped keys do the real invalidation
        with self.lock:
            self.db.execute("UPDATE count_cache SET hits=hits+1 WHERE key=?", (key,))
        return row["count"], row["universe"]

    def cache_put(self, key: str, count: int, universe: int | None, now_ts: float, max_entries: int) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO count_cache(key,count,universe,created_at,hits) VALUES(?,?,?,?,0)",
                            (key, count, universe, now_ts))
            n = self._one("SELECT count(*) FROM count_cache")[0]
            if n > max_entries:  # evict oldest 10%
                self.db.execute("DELETE FROM count_cache WHERE key IN (SELECT key FROM count_cache ORDER BY created_at LIMIT ?)",
                                (max(1, n // 10),))

    def cache_clear(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM count_cache")

    def cache_stats(self) -> dict:
        r = self._one("SELECT count(*) n, coalesce(sum(hits),0) h FROM count_cache")
        return {"entries": r["n"], "hits": r["h"]}

    def log_ingest(self, **kw) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO ingest_log(at,source,model,mode,kpis,profiles_added,rows,seconds) VALUES(?,?,?,?,?,?,?,?)",
                (utcnow_iso(), kw["source"], kw["model"], kw["mode"], json.dumps(kw["kpis"]),
                 kw["profiles_added"], kw["rows"], kw["seconds"]))


def _rule_kpis(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        if "kpi" in node:
            out.add(node["kpi"])
        for c in node.get("children", []) or []:
            out |= _rule_kpis(c)
        if node.get("child"):
            out |= _rule_kpis(node["child"])
    return out


rule_kpis = _rule_kpis
