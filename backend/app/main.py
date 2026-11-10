"""FastAPI application: JSON API + static frontend."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, uploads
from .config import ROOT, get_settings
from .llm import llm_configured
from .pipeline import run_manual_sql, run_pipeline

settings = get_settings()
FRONTEND_DIR = ROOT / "frontend"

EXAMPLES = [
    "What is the total revenue per product category?",
    "Top 5 customers by total amount spent",
    "Average salary per department",
    "How many orders were delivered each month this year?",
    "Which employees earn more than their manager?",
    "List products that have never been ordered",
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        db.init_app_tables()
        db.introspect_schema(force=True)
    except Exception as e:  # keep the API up so /api/health can explain what's wrong
        print(f"[startup] database not ready: {e}")
    yield


app = FastAPI(title="NLP2SQL Pro", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------- models ------------------------------------ #
class Turn(BaseModel):
    question: str
    sql: str | None = None


class QueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    conversation: list[Turn] = []


class ExecuteRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=8000)


# ------------------------------- routes ------------------------------------ #
@app.get("/api/health")
def health():
    db_ok = db.ping()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "llm_configured": llm_configured(),
        "model": settings.groq_model,
    }


def _schema_payload(force: bool) -> dict:
    try:
        d = db.introspect_schema(force=force).to_dict()
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {str(e)[:200]}")
    up = uploads.uploaded_names()
    for t in d["tables"]:
        t["uploaded"] = t["name"] in up
    return d


@app.get("/api/schema")
def get_schema():
    return _schema_payload(False)


@app.post("/api/schema/refresh")
def refresh_schema():
    return _schema_payload(True)


@app.get("/api/examples")
def examples():
    """Suggested questions: about the most recent uploads if any, else the demo data."""
    try:
        ups = uploads.uploaded_tables()
        if ups:
            snap = db.introspect_schema()
            out: list[str] = []
            for u in ups[:2]:
                t = next((t for t in snap.tables if t.name == u["table_name"]), None)
                if t:
                    out += uploads.suggestions_for(t)[:3]
            if out:
                return out[:6]
    except Exception:
        pass
    return EXAMPLES


@app.post("/api/upload")
def upload(
    file: UploadFile = File(...),
    table_name: str | None = Form(None),
    replace: bool = Form(False),
):
    """Load a CSV / Excel file into PostgreSQL as a new table."""
    try:
        data = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        return uploads.handle_upload(file.filename or "data.csv", data, table_name, replace)
    except uploads.UploadError as e:
        raise HTTPException(e.status, {"code": e.code, "message": str(e)})
    except Exception as e:  # database / unexpected
        raise HTTPException(500, {"code": "server", "message": f"Upload failed: {str(e)[:200]}"})


@app.get("/api/uploads")
def list_uploads():
    return uploads.uploaded_tables()


@app.delete("/api/tables/{name}")
def delete_table(name: str):
    """Remove a table that was created through Upload (never the original tables)."""
    try:
        uploads.delete_table(name)
        return {"ok": True}
    except uploads.UploadError as e:
        raise HTTPException(e.status, {"code": e.code, "message": str(e)})


@app.post("/api/query")
def query(req: QueryRequest):
    """Run the whole pipeline and return the final result (no streaming)."""
    final = None
    for ev in run_pipeline(req.question, [t.model_dump() for t in req.conversation]):
        if ev["type"] in ("result", "error"):
            final = ev
    if final is None or final["type"] == "error":
        raise HTTPException(422, (final or {}).get("message", "Unknown error"))
    return final["data"]


@app.post("/api/query/stream")
def query_stream(req: QueryRequest):
    """Same pipeline, streamed as newline-delimited JSON events for the UI."""

    def gen():
        for ev in run_pipeline(req.question, [t.model_dump() for t in req.conversation]):
            yield json.dumps(ev, default=str) + "\n"

    return StreamingResponse(
        gen(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"}
    )


@app.post("/api/execute")
def execute(req: ExecuteRequest):
    """Run user-edited SQL through the same validator + read-only executor."""
    try:
        return run_manual_sql(req.sql)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/history")
def history(limit: int = Query(40, ge=1, le=200)):
    return db.list_history(limit)


@app.delete("/api/history")
def clear_history():
    db.clear_history()
    return {"ok": True}


@app.get("/api/stats")
def stats():
    return db.history_stats()


# ------------------------------- frontend ---------------------------------- #
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")
