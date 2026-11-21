#!/usr/bin/env python3
"""Generates the four README diagram sheets (blueprint style) as self-contained HTML.

    python docs/diagrams/build.py            # writes docs/diagrams/*.html
Render to PNG with the epic-infographics `render.mjs` (see docs/diagrams/README.md).
Numbers shown on the sheets come from the README benchmark section (500k profiles x 259 KPIs).
"""
from __future__ import annotations

import base64
import os
import sys

W, H = 1600, 1000
OUT = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.environ.get("FONT_DIR", "")          # set to embed local woff2 fonts (offline rendering)

CSS = """
:root{--bg:#123A66;--bg-2:#0D3054;--line:#D8E8F8;--line-soft:rgb(216 232 248 / .45);--ink:#EAF2FB;--ink-muted:#9FB8D4;
--c1:#3E9BD6;--c2:#B8860B;--c3:#C75B9B;--c4:#6F9436;--de:#2A537F;
--fd:'Saira Condensed',sans-serif;--fb:'IBM Plex Mono',monospace}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);width:%dpx;height:%dpx;overflow:hidden}
svg{display:block}
text{font-family:var(--fb);fill:var(--ink)}
.d{font-family:var(--fd);font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.t{font-size:27px}.h{font-size:23px;font-weight:600}.b{font-size:19px}.m{font-size:18px;fill:var(--ink-muted)}
.k{font-size:17px;fill:var(--ink-muted)}.q{font-size:18px;white-space:pre}.big{font-size:150px}.mid{font-size:44px}
.box{fill:var(--bg-2);stroke:var(--line);stroke-width:2.5}
.thin{fill:none;stroke:var(--line);stroke-width:1.5}
.ln{fill:none;stroke:var(--line);stroke-width:2.2}
.dash{fill:none;stroke:var(--line);stroke-width:2;stroke-dasharray:9 7}
.soft{stroke:var(--line-soft);stroke-width:1}
""" % (W, H)


def fonts_css() -> str:
    if not FONT_DIR:
        return ""
    out = []
    for fam, wt, f in [("IBM Plex Mono", 400, "ibm-plex-mono-latin-400-normal.woff2"),
                       ("IBM Plex Mono", 500, "ibm-plex-mono-latin-500-normal.woff2"),
                       ("IBM Plex Mono", 600, "ibm-plex-mono-latin-600-normal.woff2"),
                       ("Saira Condensed", 700, "saira-condensed-latin-700-normal.woff2")]:
        p = os.path.join(FONT_DIR, f)
        b = base64.b64encode(open(p, "rb").read()).decode()
        out.append(f"@font-face{{font-family:'{fam}';font-weight:{wt};src:url(data:font/woff2;base64,{b}) format('woff2')}}")
    return "".join(out)


LINK = ('<link href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@700&'
        'family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">')

DEFS = """<defs>
<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#D8E8F8"/></marker>
<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#D8E8F8" stroke-opacity=".14" stroke-width="1"/></pattern>
<pattern id="h1" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="1.8" height="8" fill="#3E9BD6"/></pattern>
<pattern id="h2" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="1.8" height="8" fill="#B8860B"/></pattern>
<pattern id="h3" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="1.8" height="8" fill="#6F9436"/></pattern>
</defs>"""


# ----------------------------------------------------------------------------- primitives
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, cls="b", anchor="start", extra=""):
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}" {extra}>{esc(s)}</text>'


def box(x, y, w, h, title="", lines=(), accent=None, tcls="d t", pad=18, lh=26, hatch=None):
    s = [f'<rect class="box" x="{x}" y="{y}" width="{w}" height="{h}"/>']
    if hatch:
        s.append(f'<rect x="{x+3}" y="{y+3}" width="{w-6}" height="{h-6}" fill="url(#{hatch})" opacity=".35"/>')
        s.append(f'<rect x="{x+3}" y="{y+3}" width="{w-6}" height="{h-6}" fill="#0D3054" opacity=".78"/>')
    if accent:
        s.append(f'<rect x="{x}" y="{y}" width="10" height="{h}" fill="{accent}"/>')
    ty = y + 36
    if title:
        s.append(text(x + pad + (6 if accent else 0), ty, title, tcls))
        ty += 10
    for ln in lines:
        ty += lh
        cls = "b"
        if isinstance(ln, tuple):
            ln, cls = ln
        s.append(text(x + pad + (6 if accent else 0), ty, ln, cls))
    return "".join(s)


def arrow(pts, dashed=False, end=True, start=False):
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    cls = "dash" if dashed else "ln"
    me = ' marker-end="url(#ar)"' if end else ""
    ms = ' marker-start="url(#ar)"' if start else ""
    return f'<path class="{cls}" d="{d}"{me}{ms}/>'


def label(x, y, s, cls="m", anchor="middle"):
    """text with a knockout so lines break around it"""
    w = len(s) * (11.3 if cls == "b" else 10.8) + 14
    ax = x - w / 2 if anchor == "middle" else x - 7
    return (f'<rect x="{ax:.0f}" y="{y-20}" width="{w:.0f}" height="28" fill="#123A66"/>'
            + text(x, y, s, cls, anchor))


def circle_num(x, y, n):
    return (f'<circle cx="{x}" cy="{y}" r="22" fill="#123A66" stroke="#D8E8F8" stroke-width="2.5"/>'
            + text(x, y + 8, str(n), "b", "middle", 'style="font-weight:600"'))


def zone(x, y, w, h, title, pattern):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#{pattern})" opacity=".13"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" class="dash" style="stroke-opacity:.6"/>'
            + f'<rect x="{x+16}" y="{y-16}" width="{len(title)*14.5+24:.0f}" height="34" fill="#123A66"/>'
            + text(x + 28, y + 9, title, "d t"))


def frame(sheet: str, title: str, source: str):
    s = [f'<rect width="{W}" height="{H}" fill="#123A66"/><rect width="{W}" height="{H}" fill="url(#grid)"/>',
         f'<rect x="14" y="14" width="{W-28}" height="{H-28}" fill="none" stroke="#D8E8F8" stroke-width="2.5"/>',
         f'<rect x="22" y="22" width="{W-44}" height="{H-44}" fill="none" stroke="#D8E8F8" stroke-width="1"/>']
    for cx, cy in [(46, 46), (W - 46, 46), (46, H - 46)]:
        s.append(f'<path d="M{cx-9} {cy}H{cx+9}M{cx} {cy-9}V{cy+9}" stroke="#D8E8F8" stroke-opacity=".6" stroke-width="1.5"/>')
    # title block (bottom right)
    bx, by, bw, bh = W - 470, H - 150, 424, 104
    s.append(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" fill="#0D3054" stroke="#D8E8F8" stroke-width="2.5"/>'
             f'<rect x="{bx+5}" y="{by+5}" width="{bw-10}" height="{bh-10}" fill="none" stroke="#D8E8F8" stroke-width="1"/>')
    rows = [("TITLE", title), ("SHEET", sheet), ("SCALE", "NTS · mono=SQL/JSON"), ("SOURCE", source)]
    for i, (k, v) in enumerate(rows):
        yy = by + 28 + i * 21
        s.append(text(bx + 16, yy, k, "k"))
        s.append(text(bx + 104, yy, v, "k", extra='style="fill:#EAF2FB"'))
    return "".join(s)


def header(kicker, title, sub):
    return (text(60, 78, kicker, "m")
            + text(60, 128, title, "d", extra='style="font-size:56px"')
            + text(60, 162, sub, "m"))


def page(name: str, body: str):
    fonts = fonts_css()
    head = f"<style>{fonts}</style>" if fonts else LINK
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{name}</title>{head}'
            f'<style>{CSS}</style></head><body>'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">{DEFS}{body}</svg>'
            f'</body></html>')
    out_dir = os.environ.get("OUT_DIR", OUT)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, name), "w") as f:
        f.write(html)


# ----------------------------------------------------------------------------- sheet 1: architecture
def hero(x, y, big, line1, line2, line3=""):
    out = [f'<text data-hero="1" x="{x}" y="{y}" class="d" style="font-size:118px">{esc(big)}</text>']
    out.append(text(x + len(big) * 62 + 24, y - 62, line1, "h"))
    out.append(text(x + len(big) * 62 + 24, y - 32, line2, "m"))
    if line3:
        out.append(text(x + len(big) * 62 + 24, y - 6, line3, "m"))
    return "".join(out)


def sheet_architecture():
    b = [frame("1 / 4 · ARCHITECTURE", "CDP SYSTEM ARCHITECTURE", "README §2, §13"),
         header("SHEET 1 / 4 — INTERNAL WORKINGS", "System architecture",
                "control plane decides WHAT is live · data plane holds the values")]
    b.append(box(60, 186, 440, 60, "Audience builder UI", [], accent="#3E9BD6"))
    b.append(box(540, 186, 440, 60, "REST clients · BI · jobs", [], accent="#3E9BD6"))
    b.append(box(1100, 186, 440, 60, "ML jobs · S3 / Glue files", [], accent="#B8860B"))
    b.append(box(60, 284, 1480, 52, "", []))
    b.append(text(80, 318, "FASTAPI  ·  /api/count  /members  /segments  /ingest  /kpis  /admin   ·   optional X-API-Key", "b"))
    for x in (280, 760):
        b.append(arrow([(x, 246), (x, 284)]))
    b.append(zone(60, 390, 1480, 210, "CDPService", "h1"))
    subs = [("Compiler", ["rule tree → dialect SQL", "validate · normalise", "depth 8 · 100 leaves"], "#3E9BD6"),
            ("Cache", ["key = rule + KPI versions", "single-flight dedupe", "bounded · TTL safety net"], "#3E9BD6"),
            ("Ingestor", ["validate → load → publish", "new generation per load", "single writer"], "#B8860B"),
            ("Lifecycle", ["retire · purge · gc", "saved segments status", "freshness SLA"], "#B8860B")]
    for i, (t, ls, ac) in enumerate(subs):
        b.append(box(86 + i * 362, 414, 340, 164, t, ls, accent=ac, lh=27))
    b.append(arrow([(760, 336), (760, 390)]))
    b.append(zone(60, 664, 560, 176, "CONTROL PLANE", "h2"))
    b.append(box(86, 690, 508, 124, "", [], accent="#B8860B"))
    b.append(text(116, 722, "SQLite registry", "d t"))
    b.append(text(116, 752, "kpis · segments · count_cache", "b"))
    b.append(text(116, 780, "pending_gc · ingest_log · (PG in prod)", "b"))
    b.append(zone(680, 664, 860, 176, "DATA PLANE", "h1"))
    b.append(box(706, 690, 340, 124, "", [], accent="#3E9BD6"))
    b.append(text(736, 722, "Engine interface", "d t"))
    b.append(text(736, 752, "profiles + 7 KPI tables", "b"))
    b.append(text(736, 780, "(kpi_id, gen, idx, v)", "b"))
    b.append(box(1076, 700, 190, 104, "", [], hatch="h1"))
    b.append(text(1171, 746, "DuckDB", "h", "middle"))
    b.append(text(1171, 780, "in-process", "k", "middle"))
    b.append(box(1290, 700, 224, 104, "", [], hatch="h1"))
    b.append(text(1402, 746, "ClickHouse", "h", "middle"))
    b.append(text(1402, 780, "chdb / server", "k", "middle"))
    b.append(arrow([(340, 600), (340, 664)], start=True))
    b.append(arrow([(1100, 600), (1100, 664)], start=True))
    b.append(label(340, 640, "metadata", "m"))
    b.append(label(1100, 640, "values", "m"))
    b.append(arrow([(1540, 216), (1572, 216), (1572, 372), (980, 372), (980, 414)], dashed=True))
    b.append(hero(60, 944, "88 ms", "16-condition audience, cold", "500k profiles × 259 KPIs · DuckDB", "2 ms when cached"))
    return "".join(b)


# ----------------------------------------------------------------------------- sheet 2: data model
def sheet_model():
    b = [frame("2 / 4 · DATA MODEL", "CDP DATA MODEL (ERD)", "registry.py · engines/"),
         header("SHEET 2 / 4 — DATABASE MODEL", "Data model",
                "KPIs are ROWS in 7 typed tables, never columns · adding a KPI never changes the schema")]
    b.append(zone(50, 206, 700, 600, "DATA PLANE · DuckDB / ClickHouse", "h1"))
    b.append(zone(790, 206, 760, 600, "CONTROL PLANE · SQLite", "h2"))
    # profiles
    b.append(box(76, 246, 270, 146, "profiles", [("profile_idx  PK", "b"), "customer_id  VARCHAR", "active  BOOLEAN"],
                 accent="#3E9BD6", lh=27))
    b.append(box(76, 430, 270, 350, "sort key", [
        ("(kpi_id, gen,", "b"), ("  profile_idx)", "b"), ("", "b"),
        ("ClickHouse:", "m"), ("ReplacingMergeTree", "m"), ("(ver), FINAL on read", "m"), ("", "m"),
        ("DuckDB: loaded in", "m"), ("key order, zone maps", "m"), ("", "m"), ("nulls are not stored", "m")],
        accent="#3E9BD6", lh=25))
    # kpi tables
    b.append(box(420, 246, 304, 534, "kpi_* × 7", [
        ("kpi_id       INT", "b"), ("gen          INT", "b"), ("profile_idx  BIGINT", "b"),
        ("v            (typed)", "b"), ("ver  (ClickHouse)", "m"), ("", "m"),
        ("kpi_num   DOUBLE", "b"), ("kpi_str   VARCHAR", "b"), ("kpi_bool  BOOLEAN", "b"), ("kpi_ts    TIMESTAMP", "b"),
        ("kpi_list  VARCHAR[]", "b"), ("kpi_geo   lat, lon", "b"), ("kpi_json  VARCHAR", "b"),
        ("", "m"), ("one row = one value", "m"), ("of one KPI, one profile", "m")],
        accent="#3E9BD6", lh=27, hatch=None))
    # control plane
    b.append(box(816, 246, 340, 340, "kpis", [
        ("kpi_id  PK, never reused", "b"), ("name  UNIQUE", "b"), ("type", "b"),
        ("description · category", "b"), ("bu · source_model", "b"), ("status · purged", "b"),
        ("active_gen   <- live gen", "b"), ("next_gen · version", "b"), ("row_count · loaded_at", "b"),
        ("max_age_hours  (SLA)", "b"), ("retired_at · created_at", "b")],
        accent="#B8860B", lh=25))
    b.append(box(816, 616, 340, 170, "pending_gc", [
        ("kpi_id · type · gen", "b"), ("delete_after", "b"), ("grace 120 s, then delete", "m")],
        accent="#B8860B", lh=27))
    b.append(box(1210, 246, 316, 230, "segments", [
        ("id  PK", "b"), ("name · owner · bu", "b"), ("rule_json", "b"), ("kpi_versions  JSON", "b"),
        ("last_count · _at", "b"), ("created · updated", "b")],
        accent="#B8860B", lh=25))
    b.append(box(1210, 500, 316, 140, "count_cache", [
        ("key  PK  sha256", "b"), ("count · universe", "b"), ("created_at · hits", "b")],
        accent="#B8860B", lh=25))
    b.append(box(1210, 656, 316, 138, "ingest_log", [
        ("at · source · model", "b"), ("mode · kpis · rows", "b"), ("profiles_added · seconds", "b")],
        accent="#B8860B", lh=25))
    # relations
    b.append(arrow([(346, 320), (420, 320)], start=False))
    b.append(circle_num(383, 296, 1).replace('r="22"', 'r="15"').replace('y="', 'y="', 1))
    b.append(arrow([(816, 400), (724, 400)]))
    b.append(circle_num(770, 376, 2).replace('r="22"', 'r="15"'))
    b.append(arrow([(1210, 330), (1156, 330)], dashed=True))
    b.append(circle_num(1183, 306, 3).replace('r="22"', 'r="15"'))
    b.append(arrow([(986, 586), (986, 616)], dashed=True))
    b.append(hero(60, 944, "7", "typed tables hold unlimited KPIs", "add a KPI = INSERT, not DDL", "retire = metadata flip"))
    keys = ["1  profile_idx joins every KPI row", "2  kpi_id + gen = kpis.active_gen",
            "3  segments pin the KPI versions they use", "   cache key = rule + those versions"]
    for i, k in enumerate(keys):
        b.append(text(640, 878 + i * 26, k, "m"))
    return "".join(b)


# ----------------------------------------------------------------------------- sheet 3: ingestion
def sheet_ingest():
    b = [frame("3 / 4 · INGESTION", "INGESTION PIPELINE", "ingest.py · registry.py"),
         header("SHEET 3 / 4 — WRITE PATH", "Ingestion",
                "load beside the live data · flip in one transaction · delete later")]
    steps = [("SOURCE", "wide Parquet/CSV/JSONL: customer_id + a column per KPI"),
             ("VALIDATE", "ids unique · names · types · retired names, else abort"),
             ("REGISTER", "infer type · auto-register KPI · allocate gen N+1"),
             ("LOAD", "unpivot to long rows · 100 KPIs per batch · gen N+1"),
             ("CHECK", "row counts per KPI match the source"),
             ("PUBLISH", "ONE transaction flips active_gen N to N+1 for all KPIs"),
             ("GC", "old gen N deleted after the grace period (120 s)")]
    y0, h, gap = 190, 78, 10
    for i, (t, d) in enumerate(steps):
        y = y0 + i * (h + gap)
        acc = "#B8860B" if t in ("PUBLISH",) else "#3E9BD6"
        b.append(box(60, y, 770, h, "", [], accent=acc))
        b.append(circle_num(112, y + 39, i + 1))
        b.append(text(156, y + 34, t, "d t"))
        b.append(text(156, y + 62, d, "b"))
        if i < len(steps) - 1:
            b.append(arrow([(112, y + 61), (112, y + h + gap + 17)]))
    # generation flip
    b.append(zone(870, 214, 680, 330, "GENERATION FLIP", "h1"))
    fx = 1250
    b.append(text(886, 300, "queries read", "m"))
    b.append(f'<rect class="box" x="886" y="312" width="{fx-886}" height="46"/>')
    b.append(text(902, 342, "gen N  (live)", "b"))
    b.append(f'<rect class="box" x="{fx}" y="312" width="{1530-fx}" height="46" style="fill:#3E9BD6;fill-opacity:.35"/>')
    b.append(text(fx + 16, 342, "gen N+1  (live)", "b"))
    b.append(text(886, 400, "loader writes", "m"))
    b.append(f'<rect class="box" x="886" y="412" width="{fx-886}" height="46" style="fill:url(#h2)"/>')
    b.append(f'<rect x="898" y="420" width="300" height="30" fill="#0D3054"/>')
    b.append(text(908, 442, "gen N+1  (invisible)", "b"))
    b.append(f'<rect class="dash" x="{fx}" y="512" width="{1530-fx}" height="0"/>')
    b.append(text(fx + 16, 500, "gen N kept, then", "m"))
    b.append(text(fx + 16, 526, "deleted after grace", "m"))
    b.append(f'<path d="M{fx} 290V470" stroke="#D8E8F8" stroke-width="3"/>')
    b.append(label(fx, 280, "FLIP = 1 txn", "m"))
    # modes
    b.append(box(870, 580, 330, 220, "REPLACE", [("full refresh of the", "b"), ("file's KPIs", "b"), ("new generation", "b"), ("atomic flip", "b")],
                 accent="#3E9BD6", lh=27))
    b.append(box(1220, 580, 330, 220, "UPSERT", [("delta rows only", "b"), ("DuckDB: delete+insert", "b"), ("ClickHouse:", "b"), ("ReplacingMergeTree", "b")],
                 accent="#3E9BD6", lh=27))
    b.append(hero(60, 944, "24 s", "111M values loaded", "500k profiles × 259 KPIs · DuckDB", "ClickHouse (chdb): 35 s"))
    b.append(text(820, 888, "fail at any step: live data", "m"))
    b.append(text(820, 914, "untouched, orphans vacuumed", "m"))
    return "".join(b)


# ----------------------------------------------------------------------------- sheet 4: retrieval
def sheet_query():
    b = [frame("4 / 4 · RETRIEVAL", "QUERY PATH", "service.py · compiler.py"),
         header("SHEET 4 / 4 — READ PATH", "Retrieval",
                "every filter change re-counts · identical audiences are served from the cache")]
    r1 = [("RULE", ["JSON tree: AND / OR / NOT", "leaf = kpi + operator", "+ value (from the UI)"]),
          ("NORMALISE", ["flatten · dedupe · sort", "prune empty groups", "limits + BU visibility"]),
          ("SNAPSHOT", ["ONE atomic read of every", "KPI used: type, id,", "version, active_gen"]),
          ("CACHE KEY", ["sha256 of rule + KPI", "versions + time bucket", "no stale entry possible"])]
    for i, (t, ls) in enumerate(r1):
        x = 60 + i * 380
        b.append(box(x, 190, 340, 140, t, ls, accent="#3E9BD6", lh=27))
        b.append(circle_num(x + 300, 222, i + 1).replace('cx="%d"' % (x + 300), 'cx="%d"' % (x + 300)))
        if i < 3:
            b.append(arrow([(x + 340, 260), (x + 380, 260)]))
    r2 = [("COMPILE", ["each leaf = a semi-join", "on its typed KPI table", "(kpi_id, gen) from step 3"], 60, "#3E9BD6"),
          ("EXECUTE", ["DuckDB or ClickHouse", "query timeout 30 s", "engine error: HTTP 422"], 440, "#3E9BD6"),
          ("RESULT", ["count · universe · funnel", "data_as_of · warnings", "then stored in the cache"], 820, "#3E9BD6"),
          ("CACHE HIT", ["return the stored count", "in about 2 ms", "new version = new key"], 1200, "#B8860B")]
    for i, (t, ls, x, ac) in enumerate(r2):
        b.append(box(x, 396, 340, 140, t, ls, accent=ac, lh=27))
        b.append(circle_num(x + 300, 428, 5 + i))
    b.append(arrow([(400, 466), (440, 466)]))
    b.append(arrow([(780, 466), (820, 466)]))
    b.append(arrow([(1450, 330), (1450, 396)]))
    b.append(label(1450, 372, "HIT", "m"))
    b.append(arrow([(1260, 330), (1260, 366), (230, 366), (230, 396)], dashed=True))
    b.append(label(740, 366, "MISS · single-flight: identical queries compute once", "m"))
    # worked example
    b.append(zone(60, 590, 1480, 250, "WORKED EXAMPLE · age >= 30 AND has_children", "h1"))
    b.append(box(90, 650, 130, 44, "", [])); b.append(text(155, 680, "AND", "h", "middle"))
    b.append(box(290, 628, 150, 44, "", [])); b.append(text(365, 658, "age >= 30", "b", "middle"))
    b.append(box(290, 704, 200, 44, "", [])); b.append(text(390, 734, "has_children", "b", "middle"))
    b.append(arrow([(220, 672), (255, 672), (255, 650), (290, 650)]))
    b.append(arrow([(220, 672), (255, 672), (255, 726), (290, 726)]))
    sql = ["SELECT count(*) FROM profiles p",
           "WHERE p.active",
           "  AND p.profile_idx IN (",
           "    SELECT profile_idx FROM kpi_num",
           "    WHERE kpi_id=1 AND gen=3 AND v>=30)",
           "  AND p.profile_idx IN (",
           "    SELECT profile_idx FROM kpi_bool",
           "    WHERE kpi_id=52 AND gen=2 AND v)"]
    for i, ln in enumerate(sql):
        b.append(text(550, 632 + i * 25, ln, "q"))
    # funnel (real demo numbers, 500k profiles)
    rows = [("all profiles", 500000, "url(#h1)"), ("age >= 30", 399843, "url(#h1)"), ("+ has_children", 168021, "#3E9BD6")]
    for i, (lab, v, fill) in enumerate(rows):
        y = 640 + i * 62
        wbar = 440 * v / 500000
        b.append(text(1010, y, f"{lab}   {v:,}", "b"))
        b.append(f'<rect x="1010" y="{y+10}" width="{wbar:.0f}" height="24" fill="{fill if fill.startswith("url") else fill}" stroke="#D8E8F8" stroke-width="2" fill-opacity="{1 if i==2 else .9}"/>')
    b.append(text(1010, 826, "funnel from demo data · kpi ids illustrative", "k"))
    b.append(hero(60, 944, "2 ms", "cached audience count", "cold: 88 ms DuckDB · 482 ms ClickHouse", "16 conditions · 500k × 259 KPIs"))
    return "".join(b)


# ----------------------------------------------------------------------------- run
if __name__ == "__main__":
    page("01-architecture.html", sheet_architecture())
    page("02-data-model.html", sheet_model())
    page("03-ingestion.html", sheet_ingest())
    page("04-retrieval.html", sheet_query())
    print("wrote", OUT)
