"""Command line: `python -m cdp.cli <command>`; engine via CDP_ENGINE=duckdb|clickhouse."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from .config import Settings
from .service import CDPService


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cdp")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--engine", choices=["duckdb", "clickhouse"], default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic Parquet files only")
    g.add_argument("--out", default="./sample"); g.add_argument("--profiles", type=int, default=100_000)
    g.add_argument("--ml-kpis", type=int, default=0)

    d = sub.add_parser("demo", help="generate + ingest the sample organisation (5 BUs)")
    d.add_argument("--profiles", type=int, default=100_000); d.add_argument("--ml-kpis", type=int, default=0)

    i = sub.add_parser("ingest", help="ingest a wide Parquet/CSV/JSONL file")
    i.add_argument("path"); i.add_argument("--model", required=True)
    i.add_argument("--mode", choices=["replace", "upsert"], default="replace")
    i.add_argument("--bu", default="ALL"); i.add_argument("--category", default="uncategorized")
    i.add_argument("--max-age-hours", type=float, default=None)

    c = sub.add_parser("count", help="count an audience from a rule JSON file")
    c.add_argument("rule_file"); c.add_argument("--bu", default=None); c.add_argument("--explain", action="store_true")

    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)

    r = sub.add_parser("retire"); r.add_argument("--kpi"); r.add_argument("--model")
    sub.add_parser("purge"); sub.add_parser("freshness"); sub.add_parser("stats")
    sub.add_parser("refresh-segments", help="recount saved segments whose KPIs changed")
    b = sub.add_parser("bench", help="time a 15-condition query"); b.add_argument("--runs", type=int, default=5)

    a = ap.parse_args(argv)
    st = Settings()
    if a.data_dir:
        st.data_dir = a.data_dir
    if a.engine:
        st.engine = a.engine

    if a.cmd == "generate":
        from .datagen import generate_core, generate_ml_model
        os.makedirs(a.out, exist_ok=True)
        print(generate_core(os.path.join(a.out, "core.parquet"), a.profiles))
        if a.ml_kpis:
            print(generate_ml_model(os.path.join(a.out, "ml_demo.parquet"), "demo", a.ml_kpis, a.profiles))
        return 0

    svc = CDPService(st)
    if a.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(svc, st), host=a.host, port=a.port)
    elif a.cmd == "demo":
        from .datagen import CORE_META, generate_core, generate_ml_model
        with tempfile.TemporaryDirectory() as t:
            core = os.path.join(t, "core.parquet")
            generate_core(core, a.profiles)
            r = svc.ingest(core, model="core", meta=CORE_META)
            print(f"core: {len(r.kpis)} KPIs, {r.rows:,} values, {r.profiles_added:,} profiles in {r.seconds}s")
            if a.ml_kpis:
                ml = os.path.join(t, "ml.parquet")
                generate_ml_model(ml, "demo", a.ml_kpis, a.profiles)
                r = svc.ingest(ml, model="ml_demo", category="ml")
                print(f"ml_demo: {len(r.kpis)} KPIs, {r.rows:,} values in {r.seconds}s")
    elif a.cmd == "ingest":
        r = svc.ingest(a.path, model=a.model, mode=a.mode, bu=a.bu, category=a.category, max_age_hours=a.max_age_hours)
        print(json.dumps({"kpis": len(r.kpis), "rows": r.rows, "profiles_added": r.profiles_added, "seconds": r.seconds}))
    elif a.cmd == "count":
        print(json.dumps(svc.count(json.load(open(a.rule_file)), bu=a.bu, explain=a.explain), indent=2, default=str))
    elif a.cmd == "retire":
        print(json.dumps(svc.retire_model(a.model) if a.model else svc.retire_kpi(a.kpi), default=str, indent=2))
    elif a.cmd == "purge":
        print(svc.purge())
    elif a.cmd == "freshness":
        print(json.dumps(svc.freshness_report(), indent=2, default=str))
    elif a.cmd == "stats":
        print(json.dumps(svc.stats(), indent=2))
    elif a.cmd == "refresh-segments":
        print([s["id"] for s in svc.refresh_stale_segments()])
    elif a.cmd == "bench":
        from .benchmark import run_bench
        run_bench(svc, a.runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
