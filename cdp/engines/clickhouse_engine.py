"""ClickHouse engine.

Two modes behind one class:
  * embedded (default, no URL): `chdb`, ClickHouse running in-process. Used for
    dev and for the test-suite so the exact ClickHouse SQL is exercised in CI.
    chdb is a process-wide singleton (one data path per process): fine for dev/CI,
    not a production topology.
  * server (CDP_CLICKHOUSE_URL=http://user:pw@host:8123/db): `clickhouse-connect`
    over HTTP. Data must be staged on S3/HTTP because the server cannot read
    this process's local disk. (Server mode is implemented but NOT exercised by
    the test-suite in this repo; see README "Risks".)

All tables are ReplacingMergeTree(ver) keyed by (kpi_id, gen, profile_idx):
re-inserting a row with a higher `ver` is an upsert, and every read uses FINAL
so duplicates are never visible.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlparse

from .base import Engine
from .dialect import ClickHouseDialect

_KPI = "kpi_id UInt32, gen UInt32, profile_idx UInt64, {cols}, ver UInt64"
DDL = [
    "CREATE TABLE IF NOT EXISTS profiles(profile_idx UInt64, customer_id String, active Bool, ver UInt64) "
    "ENGINE = ReplacingMergeTree(ver) ORDER BY profile_idx",
] + [
    f"CREATE TABLE IF NOT EXISTS {t}({_KPI.format(cols=c)}) ENGINE = ReplacingMergeTree(ver) "
    f"ORDER BY (kpi_id, gen, profile_idx)"
    for t, c in [
        ("kpi_num", "v Float64"), ("kpi_str", "v String"), ("kpi_bool", "v Bool"),
        ("kpi_ts", "v DateTime64(6, 'UTC')"), ("kpi_list", "v Array(String)"),
        ("kpi_geo", "lat Float64, lon Float64"), ("kpi_json", "v String"),
    ]
]


class ClickHouseEngine(Engine):
    name = "clickhouse"
    dialect = ClickHouseDialect()

    def __init__(self, path: str | None = None, url: str = ""):
        self.lock = threading.RLock()     # chdb sessions are not thread-safe; server mode ignores it
        self.url = url
        if url:
            import clickhouse_connect
            u = urlparse(url)
            self.client = clickhouse_connect.get_client(
                host=u.hostname, port=u.port or 8123, username=u.username or "default",
                password=u.password or "", database=(u.path or "/default").strip("/") or "default")
            self.sess = None
        else:
            from chdb import session
            self.sess = session.Session(path) if path else session.Session()
            self.client = None

    # ---- plumbing
    def _run(self, sql: str, read: bool = False) -> list[tuple]:
        if self.client is not None:
            if read:
                return [tuple(r) for r in self.client.query(sql).result_rows]
            self.client.command(sql)
            return []
        with self.lock:
            if read:
                raw = self.sess.query(sql, "JSONCompact")
                txt = raw.bytes().decode() if hasattr(raw, "bytes") else str(raw)
                if not txt.strip():
                    return []
                return [tuple(r) for r in json.loads(txt)["data"]]
            self.sess.query(sql)
            return []

    def _src(self, path: str) -> str:
        if self.client is None:
            return "file('" + path.replace("\\", "\\\\").replace("'", "\\'") + "', 'Parquet')"
        if path.startswith(("s3://", "http://", "https://")):
            return "s3('" + path + "', 'Parquet')" if path.startswith("s3://") else "url('" + path + "', 'Parquet')"
        raise ValueError("ClickHouse server mode needs staged data on s3:// or http(s)://")

    @staticmethod
    def _ver() -> int:
        return time.time_ns() // 1000

    def reset(self):
        """Drop and recreate every table (tests / dev). chdb is a process-wide singleton, so this is how
        independent test cases get a clean slate."""
        for t in ["profiles", "kpi_num", "kpi_str", "kpi_bool", "kpi_ts", "kpi_list", "kpi_geo", "kpi_json"]:
            self._run(f"DROP TABLE IF EXISTS {t} SYNC")
        self.init_schema()

    def init_schema(self):
        for s in DDL:
            self._run(s)

    def close(self):
        if self.client:
            self.client.close()
        elif self.sess:
            self.sess.close()

    def query(self, sql, timeout_s=None):
        if timeout_s and sql.lstrip().upper().startswith("SELECT"):
            sql += f" SETTINGS max_execution_time = {int(timeout_s)}, output_format_json_quote_64bit_integers = 0"
        else:
            sql += " SETTINGS output_format_json_quote_64bit_integers = 0"
        return self._run(sql, read=True)

    def add_profiles(self, stage_path):
        before = self._run("SELECT count() FROM profiles FINAL", read=True)[0][0]
        self._run(f"""
            INSERT INTO profiles
            SELECT (SELECT coalesce(max(profile_idx), 0) FROM profiles) + row_number() OVER (ORDER BY customer_id),
                   customer_id, true, {self._ver()}
            FROM (SELECT DISTINCT customer_id FROM {self._src(stage_path)}) AS s
            WHERE customer_id NOT IN (SELECT customer_id FROM profiles)
            SETTINGS use_structure_from_insertion_table_in_table_functions = 0""")
        return self._run("SELECT count() FROM profiles FINAL", read=True)[0][0] - before

    def load_long(self, kpi_type, stage_path, ver):
        t = self.table(kpi_type)
        vcols = {"geo": "s.lat, s.lon", "list": "CAST(s.v, 'Array(String)')"}.get(kpi_type, "s.v")
        self._run(f"""
            INSERT INTO {t}
            SELECT s.kpi_id, s.gen, p.profile_idx, {vcols}, {self._ver()}
            FROM {self._src(stage_path)} AS s INNER JOIN profiles AS p ON p.customer_id = s.customer_id
            SETTINGS use_structure_from_insertion_table_in_table_functions = 0""")

    def delete_for_upsert(self, kpi_type, stage_path):
        return None   # ReplacingMergeTree: a newer `ver` for the same key replaces the old row at read time

    def count_rows_many(self, kpi_type, pairs):
        if not pairs:
            return {}
        tup = ",".join(f"({int(k)},{int(g)})" for k, g in pairs)
        rows = self._run(f"SELECT kpi_id, count() FROM {self.table(kpi_type)} FINAL "
                         f"WHERE (kpi_id, gen) IN ({tup}) GROUP BY kpi_id", read=True)
        out = {int(k): 0 for k, _ in pairs}
        out.update({int(k): int(n) for k, n in rows})
        return out

    def vacuum_many(self, kpi_type, keep):
        if not keep:
            return
        ids = ",".join(str(int(k)) for k in keep)
        tup = ",".join(f"({int(k)},{int(g)})" for k, g in keep.items())
        self._run(f"ALTER TABLE {self.table(kpi_type)} DELETE WHERE kpi_id IN ({ids}) AND (kpi_id, gen) NOT IN ({tup}) "
                  f"SETTINGS mutations_sync = 1")

    def drop_generations(self, kpi_type, pairs):
        if pairs:
            tup = ",".join(f"({int(k)},{int(g)})" for k, g in pairs)
            self._run(f"ALTER TABLE {self.table(kpi_type)} DELETE WHERE (kpi_id, gen) IN ({tup}) SETTINGS mutations_sync = 1")

    def purge(self, ids_by_type):
        for t, ids in ids_by_type.items():
            if ids:
                self._run(f"ALTER TABLE {self.table(t)} DELETE WHERE kpi_id IN ({','.join(str(int(i)) for i in ids)}) "
                          f"SETTINGS mutations_sync = 1")

    def stats(self) -> dict[str, Any]:
        out = {"profiles": self._run("SELECT count() FROM profiles FINAL", read=True)[0][0]}
        for t in ("kpi_num", "kpi_str", "kpi_bool", "kpi_ts", "kpi_list", "kpi_geo", "kpi_json"):
            out[t] = self._run(f"SELECT count() FROM {t} FINAL", read=True)[0][0]
        return out
