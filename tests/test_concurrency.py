"""Readers hammering the service while a writer repeatedly refreshes a KPI.
Invariant: every answer equals the old or the new generation, never a mix, and nothing raises."""
from __future__ import annotations

import threading

from tests.conftest import NOW
from tests.helpers import write_parquet

IDS = [f"c{i}" for i in range(200)]
RULE = {"op": "AND", "children": [{"kpi": "score", "operator": "gt", "value": 0.5},
                                  {"kpi": "other", "operator": "gt", "value": 0.5}]}


def test_refresh_during_reads_is_atomic(fresh, tmp_path):
    # state X: score high / other low.  state Y: score low / other high.  The rule needs BOTH high:
    # a consistent snapshot (all X or all Y) always yields 0; a torn read (one KPI new, one old) yields 200.
    hi = write_parquet(tmp_path / "hi.parquet", customer_id=IDS, score=[0.9] * 200, other=[0.1] * 200)
    lo = write_parquet(tmp_path / "lo.parquet", customer_id=IDS, score=[0.1] * 200, other=[0.9] * 200)
    fresh.ingest(hi, model="m")
    seen, errors, stop = set(), [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                seen.add(fresh.count(RULE, now=NOW, use_cache=False)["count"])
            except Exception as e:                       # pragma: no cover - the test fails below
                errors.append(repr(e))

    readers = [threading.Thread(target=reader) for _ in range(4)]
    [t.start() for t in readers]
    for i in range(30):
        fresh.ingest(lo if i % 2 == 0 else hi, model="m")
    stop.set()
    [t.join() for t in readers]
    assert not errors, errors[:3]
    assert seen == {0}, f"a reader saw a half-refreshed state (count {seen}); KPIs of one file must flip together"
    assert seen                                                               # readers actually ran
