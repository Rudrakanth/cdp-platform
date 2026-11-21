from __future__ import annotations

import os

from ..config import Settings
from .base import Engine


def make_engine(settings: Settings) -> Engine:
    """Engine factory: CDP_ENGINE=duckdb|clickhouse."""
    if settings.engine == "duckdb":
        from .duckdb_engine import DuckDBEngine
        os.makedirs(settings.data_dir, exist_ok=True)
        return DuckDBEngine(os.path.join(settings.data_dir, "cdp.duckdb"))
    if settings.engine == "clickhouse":
        from .clickhouse_engine import ClickHouseEngine
        if settings.clickhouse_url:
            return ClickHouseEngine(url=settings.clickhouse_url)
        os.makedirs(settings.data_dir, exist_ok=True)
        return ClickHouseEngine(path=os.path.join(settings.data_dir, "chdb"))
    raise ValueError(f"unknown engine '{settings.engine}'")
