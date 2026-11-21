"""Runtime settings, all overridable through environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    data_dir: str = field(default_factory=lambda: _env("CDP_DATA_DIR", "./data"))
    engine: str = field(default_factory=lambda: _env("CDP_ENGINE", "duckdb"))  # duckdb | clickhouse
    # clickhouse: embedded (chdb) when no URL is given, otherwise HTTP server mode
    clickhouse_url: str = field(default_factory=lambda: _env("CDP_CLICKHOUSE_URL", ""))
    # cache
    cache_ttl_s: int = field(default_factory=lambda: int(_env("CDP_CACHE_TTL_S", "21600")))
    cache_max_entries: int = field(default_factory=lambda: int(_env("CDP_CACHE_MAX", "50000")))
    # query guards
    max_depth: int = 8
    max_leaves: int = 100
    max_in_list: int = 10_000
    query_timeout_s: float = field(default_factory=lambda: float(_env("CDP_QUERY_TIMEOUT_S", "30")))
    # superseded generations are physically deleted only after this grace period, so a query that
    # resolved the old generation just before a refresh can still finish against intact data
    gc_grace_s: float = field(default_factory=lambda: float(_env("CDP_GC_GRACE_S", "120")))
    max_upload_mb: int = field(default_factory=lambda: int(_env("CDP_MAX_UPLOAD_MB", "500")))
    business_units: tuple = ("RETAIL", "FASHION", "GROCERY", "FINANCE", "TRAVEL")

    def path(self, *parts: str) -> str:
        p = os.path.join(self.data_dir, *parts)
        os.makedirs(os.path.dirname(p) if os.path.splitext(p)[1] else p, exist_ok=True)
        return p
