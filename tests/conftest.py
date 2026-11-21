from __future__ import annotations

import os
from datetime import datetime

import pytest

from cdp.config import Settings
from cdp.datagen import CORE_META, generate_core
from cdp.engines.clickhouse_engine import ClickHouseEngine
from cdp.engines.duckdb_engine import DuckDBEngine
from cdp.registry import Registry
from cdp.service import CDPService
from tests.oracle import load_rows

NOW = datetime(2026, 11, 1, 12, 0, 0)     # fixed "now" so relative-time filters are deterministic
N_PROFILES = 3000
ENGINES = ["duckdb", "clickhouse"]


@pytest.fixture(scope="session")
def core_path(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("data") / "core.parquet")
    generate_core(p, N_PROFILES, ref_now=datetime(2026, 11, 1))
    return p


@pytest.fixture(scope="session")
def core_rows(core_path):
    return load_rows(core_path)


@pytest.fixture(scope="session")
def chdb_path(tmp_path_factory):
    return str(tmp_path_factory.mktemp("chdb"))


def make_service(engine: str, tmp, chdb_path: str, **settings) -> CDPService:
    s = Settings(data_dir=str(tmp), **settings)
    if engine == "duckdb":
        eng = DuckDBEngine()
    else:
        eng = ClickHouseEngine(path=chdb_path)
        eng.reset()
    return CDPService(s, engine=eng, registry=Registry())


@pytest.fixture(scope="module", params=ENGINES)
def loaded(request, core_path, tmp_path_factory, chdb_path):
    """Full core dataset ingested into each engine (module-scoped: read-only tests only)."""
    svc = make_service(request.param, tmp_path_factory.mktemp("svc"), chdb_path)
    svc.ingest(core_path, model="core", meta=CORE_META)
    yield svc


@pytest.fixture(params=ENGINES)
def fresh(request, tmp_path, chdb_path):
    """Empty service per test (mutating tests)."""
    return make_service(request.param, tmp_path, chdb_path)
