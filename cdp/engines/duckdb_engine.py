"""DuckDB engine: embedded, zero-ops, reads/writes Parquet on local disk or S3.

Concurrency: DuckDB gives MVCC between connections of one process. Reads use
per-call cursors; writes are serialized by a lock and run in a transaction.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any

import duckdb

from .base import Engine
from .dialect import DuckDialect

DDL = [
    "CREATE TABLE IF NOT EXISTS profiles(profile_idx BIGINT, customer_id VARCHAR, active BOOLEAN)",
    "CREATE TABLE IF NOT EXISTS kpi_num (kpi_id INTEGER, gen INTEGER, profile_idx BIGINT, v DOUBLE)",
    "CREATE TABLE IF NOT EXISTS kpi_str (kpi_id INTEGER, gen INTEGER, profile_idx BIGINT, v VARCHAR)",
    "CREATE TABLE IF NOT EXISTS kpi_bool(kpi_id INTEGER, gen INTEGER, profile_idx BIGINT, v BOOLEAN)",
    "CREATE TABLE IF NOT EXISTS kpi_ts  (kpi_id INTEGER, gen INTEGER, profile_idx BIGINT, v TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS kpi_list(kpi_id INTEGER, gen INTEGER, profile_idx BIGINT, v VARCHAR[])",
    "CREATE TABLE IF NOT EXISTS kpi_geo (kpi_id INTEGER, gen INTEGER, profile_idx BIGINT, lat DOUBLE, lon DOUBLE)",
    "CREATE TABLE IF NOT EXISTS kpi_json(kpi_id INTEGER, gen INTEGER, profile_idx BIGINT, v VARCHAR)",
]


class DuckDBEngine(Engine):
    name = "duckdb"
    dialect = DuckDialect()

    def __init__(self, path: str = ":memory:"):
        self.con = duckdb.connect(path)
        self.wlock = threading.RLock()

    def init_schema(self):
        with self.wlock:
            for s in DDL:
                self.con.execute(s)

    def close(self):
        self.con.close()

    @contextlib.contextmanager
    def atomic(self):
        with self.wlock:
            self.con.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
            else:
                self.con.execute("COMMIT")

    def query(self, sql, timeout_s=None):
        cur = self.con.cursor()
        timer = None
        if timeout_s:
            timer = threading.Timer(timeout_s, cur.interrupt)
            timer.start()
        try:
            return cur.execute(sql).fetchall()
        except duckdb.InterruptException:
            raise TimeoutError(f"query exceeded {timeout_s}s")
        finally:
            if timer:
                timer.cancel()
            cur.close()

    @staticmethod
    def _q(path: str) -> str:
        return "'" + path.replace("'", "''") + "'"

    def add_profiles(self, stage_path):
        with self.wlock:
            before = self.con.execute("SELECT count(*) FROM profiles").fetchone()[0]
            self.con.execute(f"""
                INSERT INTO profiles
                SELECT (SELECT coalesce(max(profile_idx), 0) FROM profiles) + row_number() OVER (ORDER BY customer_id),
                       customer_id, true
                FROM (SELECT DISTINCT customer_id FROM read_parquet({self._q(stage_path)})) s
                WHERE customer_id NOT IN (SELECT customer_id FROM profiles)""")
            return self.con.execute("SELECT count(*) FROM profiles").fetchone()[0] - before

    def load_long(self, kpi_type, stage_path, ver):
        t = self.table(kpi_type)
        cols = "s.lat, s.lon" if kpi_type == "geo" else "s.v"
        with self.wlock:
            self.con.execute(f"""
                INSERT INTO {t}
                SELECT s.kpi_id, s.gen, p.profile_idx, {cols}
                FROM read_parquet({self._q(stage_path)}) s JOIN profiles p ON p.customer_id = s.customer_id
                ORDER BY s.kpi_id, p.profile_idx""")      # clustered by (kpi_id, profile_idx) -> zone-map friendly

    def delete_for_upsert(self, kpi_type, stage_path):
        with self.wlock:
            self.con.execute(f"""
                DELETE FROM {self.table(kpi_type)} WHERE (kpi_id, gen, profile_idx) IN
                (SELECT s.kpi_id, s.gen, p.profile_idx FROM read_parquet({self._q(stage_path)}) s
                 JOIN profiles p ON p.customer_id = s.customer_id)""")

    def count_rows_many(self, kpi_type, pairs):
        if not pairs:
            return {}
        tup = ",".join(f"({int(k)},{int(g)})" for k, g in pairs)
        rows = self.con.cursor().execute(
            f"SELECT kpi_id, count(*) FROM {self.table(kpi_type)} WHERE (kpi_id, gen) IN ({tup}) GROUP BY kpi_id").fetchall()
        out = {int(k): 0 for k, _ in pairs}
        out.update({int(k): int(n) for k, n in rows})
        return out

    def vacuum_many(self, kpi_type, keep):
        if not keep:
            return
        ids = ",".join(str(int(k)) for k in keep)
        tup = ",".join(f"({int(k)},{int(g)})" for k, g in keep.items())
        with self.wlock:
            self.con.execute(f"DELETE FROM {self.table(kpi_type)} WHERE kpi_id IN ({ids}) AND (kpi_id, gen) NOT IN ({tup})")

    def drop_generations(self, kpi_type, pairs):
        if pairs:
            tup = ",".join(f"({int(k)},{int(g)})" for k, g in pairs)
            with self.wlock:
                self.con.execute(f"DELETE FROM {self.table(kpi_type)} WHERE (kpi_id, gen) IN ({tup})")

    def purge(self, ids_by_type):
        with self.wlock:
            for t, ids in ids_by_type.items():
                if ids:
                    self.con.execute(f"DELETE FROM {self.table(t)} WHERE kpi_id IN ({','.join(str(int(i)) for i in ids)})")

    def stats(self) -> dict[str, Any]:
        cur = self.con.cursor()
        out = {"profiles": cur.execute("SELECT count(*) FROM profiles").fetchone()[0]}
        for t in ("kpi_num", "kpi_str", "kpi_bool", "kpi_ts", "kpi_list", "kpi_geo", "kpi_json"):
            out[t] = cur.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        return out
