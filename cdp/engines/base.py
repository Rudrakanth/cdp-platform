"""Engine interface. The service talks only to this; DuckDB and ClickHouse
implement it. Both share the same logical schema and the same compiled SQL
shape, which is what makes the engines swappable."""
from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from typing import Any

from ..types import TABLE_FOR


class Engine(ABC):
    name = "base"
    dialect = None

    # ---- lifecycle
    @abstractmethod
    def init_schema(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...

    # ---- reads
    @abstractmethod
    def query(self, sql: str, timeout_s: float | None = None) -> list[tuple]: ...

    # ---- writes (all take staged Parquet produced by the ingest layer)
    @abstractmethod
    def add_profiles(self, stage_path: str) -> int:
        """Insert customer_ids from stage that do not exist yet. Returns #added."""

    @abstractmethod
    def load_long(self, kpi_type: str, stage_path: str, ver: int) -> None:
        """Stage layout: customer_id, kpi_id, gen, (v | lat, lon). Joins to profiles and inserts."""

    @abstractmethod
    def delete_for_upsert(self, kpi_type: str, stage_path: str) -> None:
        """Remove existing (kpi_id, gen, profile) rows that the stage is about to overwrite."""

    @abstractmethod
    def count_rows_many(self, kpi_type: str, pairs: list[tuple[int, int]]) -> dict[int, int]:
        """{kpi_id: row count} for the given (kpi_id, gen) pairs."""

    @abstractmethod
    def vacuum_many(self, kpi_type: str, keep: dict[int, int]) -> None:
        """For each kpi_id drop every generation except keep[kpi_id] (use 0 to drop all)."""

    @abstractmethod
    def drop_generations(self, kpi_type: str, pairs: list[tuple[int, int]]) -> None:
        """Delete exactly these (kpi_id, gen) generations."""

    @abstractmethod
    def purge(self, ids_by_type: dict[str, list[int]]) -> None:
        """Physically delete all rows of retired KPIs."""

    @abstractmethod
    def stats(self) -> dict[str, Any]: ...

    @contextlib.contextmanager
    def atomic(self):
        """Group writes so readers never see half of them (no-op where the engine cannot)."""
        yield

    # helpers
    @staticmethod
    def table(kpi_type: str) -> str:
        return TABLE_FOR[kpi_type]
