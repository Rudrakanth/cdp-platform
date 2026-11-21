"""HTTP API + static UI. Run: `python -m cdp.cli serve` (or `uvicorn --factory cdp.api:create_app`)."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from typing import Any, Optional

from fastapi import Body, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .errors import CDPError, ValidationError
from .service import CDPService
from .types import KPI_TYPES, OPERATORS, VALUE_SHAPE, NO_VALUE

UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
UPLOAD_EXT = (".parquet", ".csv", ".tsv", ".jsonl", ".ndjson", ".json")


class CountReq(BaseModel):
    rule: dict = Field(default_factory=dict)
    bu: Optional[str] = None
    explain: bool = False
    use_cache: bool = True
    now: Optional[datetime] = None          # tests / what-if; defaults to server time


class BreakdownReq(BaseModel):
    rule: dict = Field(default_factory=dict)
    kpi: str
    bu: Optional[str] = None
    limit: int = 8
    now: Optional[datetime] = None


class MembersReq(BaseModel):
    rule: dict = Field(default_factory=dict)
    bu: Optional[str] = None
    limit: int = 50
    offset: int = 0
    now: Optional[datetime] = None


class SegmentReq(BaseModel):
    name: str
    rule: dict
    owner: str = ""
    bu: str = ""


class JsonIngestReq(BaseModel):
    records: list[dict]
    model: str
    mode: str = "upsert"
    bu: str = "ALL"
    category: str = "uncategorized"


class PathIngestReq(BaseModel):
    path: str
    model: str
    mode: str = "replace"
    bu: str = "ALL"
    category: str = "uncategorized"
    max_age_hours: Optional[float] = None


class DemoReq(BaseModel):
    profiles: int = 20000
    ml_kpis: int = 0


def create_app(service: Optional[CDPService] = None, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    svc = service or CDPService(settings)
    api_key = os.environ.get("CDP_API_KEY", "")
    app = FastAPI(title="CDP Audience Platform", version="1.0")
    app.state.svc = svc

    @app.exception_handler(CDPError)
    async def _cdp_err(_: Request, e: CDPError):
        return JSONResponse({"error": type(e).__name__, "detail": str(e)}, status_code=e.status)

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        # Optional shared-secret guard. A real SaaS puts OIDC/JWT here (see README, Risks).
        if api_key and request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if request.headers.get("x-api-key") != api_key:
                return JSONResponse({"error": "Unauthorized", "detail": "missing or wrong X-API-Key"}, status_code=401)
        return await call_next(request)

    # ---------------------------------------------------------------- meta
    @app.get("/api/health")
    def health():
        return {"status": "ok", "engine": svc.engine.name}

    @app.get("/api/meta")
    def meta():
        return {"types": KPI_TYPES, "operators": OPERATORS, "shapes": VALUE_SHAPE, "no_value": sorted(NO_VALUE),
                "business_units": list(svc.s.business_units), "engine": svc.engine.name}

    @app.get("/api/kpis")
    def kpis(bu: Optional[str] = None, category: Optional[str] = None, q: Optional[str] = None,
             model: Optional[str] = None, include_retired: bool = False):
        out = svc.list_kpis(bu=bu, category=category, model=model, include_retired=include_retired)
        if q:
            ql = q.lower()
            out = [k for k in out if ql in k["name"] or ql in (k["description"] or "").lower()]
        return out

    @app.get("/api/kpis/{name}/values")
    def kpi_values(name: str, q: str = "", limit: int = 20, bu: Optional[str] = None):
        """Type-ahead suggestions for string / list KPIs."""
        return svc.suggest_values(name, q, limit=limit, bu=bu)

    # ------------------------------------------------------------ querying
    @app.post("/api/count")
    def count(r: CountReq):
        return svc.count(r.rule, bu=r.bu, explain=r.explain, use_cache=r.use_cache, now=r.now)

    @app.post("/api/breakdown")
    def breakdown(r: BreakdownReq):
        """Total vs audience split of one KPI (powers the charts)."""
        return svc.breakdown(r.rule, r.kpi, bu=r.bu, limit=r.limit, now=r.now)

    @app.post("/api/members")
    def members(r: MembersReq):
        return svc.members(r.rule, bu=r.bu, limit=r.limit, offset=r.offset, now=r.now)

    @app.get("/api/profile/{customer_id}")
    def profile(customer_id: str, bu: Optional[str] = None):
        return svc.profile(customer_id, bu=bu)

    # ------------------------------------------------------------ segments
    @app.get("/api/segments")
    def segments(bu: Optional[str] = None, owner: Optional[str] = None):
        return svc.list_segments(bu, owner)

    @app.post("/api/segments")
    def seg_create(r: SegmentReq):
        return svc.save_segment(r.name, r.rule, owner=r.owner, bu=r.bu)

    @app.put("/api/segments/{seg_id}")
    def seg_update(seg_id: int, r: SegmentReq):
        return svc.save_segment(r.name, r.rule, owner=r.owner, bu=r.bu, seg_id=seg_id)

    @app.delete("/api/segments/{seg_id}")
    def seg_delete(seg_id: int):
        svc.delete_segment(seg_id)
        return {"deleted": seg_id}

    @app.post("/api/segments/{seg_id}/refresh")
    def seg_refresh(seg_id: int):
        return svc.refresh_segment(seg_id)

    @app.post("/api/segments/refresh-stale")
    def seg_refresh_stale():
        return {"refreshed": [s["id"] for s in svc.refresh_stale_segments()]}

    # ----------------------------------------------------------- ingestion
    def _res(r):
        return {"mode": r.mode, "model": r.model, "profiles_added": r.profiles_added, "rows": r.rows,
                "seconds": r.seconds, "kpis": r.kpis}

    @app.post("/api/ingest")
    async def ingest_upload(request: Request, filename: str, model: str, mode: str = "replace", bu: str = "ALL",
                            category: str = "uncategorized", max_age_hours: Optional[float] = None):
        """Raw-body upload (no multipart dependency). Body = the file bytes."""
        ext = os.path.splitext(filename)[1].lower()
        if ext not in UPLOAD_EXT:
            raise ValidationError(f"unsupported file type {ext}; use {', '.join(UPLOAD_EXT)}")
        fd, path = tempfile.mkstemp(suffix=ext, prefix="cdp_upload_")
        size, cap = 0, svc.s.max_upload_mb * 1024 * 1024
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cap:
                        raise ValidationError(f"upload larger than {svc.s.max_upload_mb} MB")
                    f.write(chunk)
            res = await run_in_threadpool(svc.ingest, path, model=model, mode=mode, bu=bu, category=category,
                                          max_age_hours=max_age_hours)
            return _res(res)
        finally:
            os.unlink(path)

    @app.post("/api/ingest/path")
    def ingest_path(r: PathIngestReq):
        """Server-side source. Only s3:// / https:// or files under <data_dir>/inbox (no arbitrary file reads)."""
        p = r.path
        if not p.startswith(("s3://", "https://")):
            inbox = os.path.realpath(os.path.join(svc.s.data_dir, "inbox"))
            real = os.path.realpath(p if os.path.isabs(p) else os.path.join(inbox, p))
            if not real.startswith(inbox + os.sep):
                raise ValidationError(f"local sources must be inside {inbox}")
            p = real
        return _res(svc.ingest(p, model=r.model, mode=r.mode, bu=r.bu, category=r.category,
                               max_age_hours=r.max_age_hours))

    @app.post("/api/ingest/json")
    def ingest_json(r: JsonIngestReq):
        return _res(svc.ingest_records(r.records, model=r.model, mode=r.mode, bu=r.bu, category=r.category))

    # ----------------------------------------------------------- lifecycle
    @app.post("/api/kpis/{name}/retire")
    def retire(name: str):
        return svc.retire_kpi(name)

    @app.post("/api/models/{model}/retire")
    def retire_model(model: str):
        return svc.retire_model(model)

    @app.post("/api/admin/purge")
    def purge():
        return svc.purge()

    @app.get("/api/admin/freshness")
    def freshness():
        return svc.freshness_report()

    @app.get("/api/stats")
    def stats():
        return svc.stats()

    @app.post("/api/admin/cache/clear")
    def cache_clear():
        svc.reg.cache_clear()
        return {"cleared": True}

    @app.post("/api/admin/demo")
    def demo(r: DemoReq = Body(default=DemoReq())):
        """Generate + ingest the sample organisation (5 BUs) so the UI has something to query."""
        from .datagen import CORE_META, generate_core, generate_ml_model
        if not 100 <= r.profiles <= 5_000_000:
            raise ValidationError("profiles must be 100..5,000,000")
        with tempfile.TemporaryDirectory() as d:
            core = os.path.join(d, "core.parquet")
            generate_core(core, r.profiles)
            out = [_res(svc.ingest(core, model="core", meta=CORE_META))]
            if r.ml_kpis:
                ml = os.path.join(d, "ml.parquet")
                generate_ml_model(ml, "demo", r.ml_kpis, r.profiles)
                out.append(_res(svc.ingest(ml, model="ml_demo", category="ml")))
        return {"ingested": out}

    # ------------------------------------------------------------------ UI
    @app.get("/")
    def index():
        return FileResponse(os.path.join(UI_DIR, "index.html"))

    return app
