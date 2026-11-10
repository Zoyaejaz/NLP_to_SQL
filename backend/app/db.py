"""PostgreSQL access: engines, live schema introspection, safe read-only execution,
and the app's own history table (kept in a separate `app` schema)."""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from .config import get_settings

_settings = get_settings()

# Admin engine (history writes, introspection, seeding)
# client_encoding=utf8 so accented / non-English text round-trips even if the server DB is SQL_ASCII
_CONNECT_ARGS = {"client_encoding": "utf8"}
engine: Engine = create_engine(
    _settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5,
    connect_args=_CONNECT_ARGS,
)
# Execution engine for LLM-generated SQL. Ideally a SELECT-only role.
exec_engine: Engine = (
    create_engine(
        _settings.readonly_database_url, pool_pre_ping=True, pool_size=5, max_overflow=5,
        connect_args=_CONNECT_ARGS,
    )
    if _settings.readonly_database_url
    else engine
)


# --------------------------------------------------------------------------- #
# Schema introspection
# --------------------------------------------------------------------------- #
@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    primary_key: bool = False
    references: str | None = None  # "table.column"
    sample_values: list[str] = field(default_factory=list)


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    row_estimate: int | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "row_estimate": self.row_estimate,
            "columns": [c.__dict__ for c in self.columns],
        }


@dataclass
class SchemaSnapshot:
    tables: list[TableInfo]
    created_at: float

    @property
    def table_names(self) -> set[str]:
        return {t.name.lower() for t in self.tables}

    def column_names(self, table: str) -> set[str]:
        for t in self.tables:
            if t.name.lower() == table.lower():
                return {c.name.lower() for c in t.columns}
        return set()

    @property
    def fingerprint(self) -> str:
        raw = json.dumps([t.to_dict() for t in self.tables], sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_prompt(self) -> str:
        """Compact DDL-like text for the LLM."""
        lines: list[str] = []
        for t in self.tables:
            lines.append(f"TABLE {t.name}" + (f"  -- ~{t.row_estimate} rows" if t.row_estimate else ""))
            for c in t.columns:
                bits = [f"  {c.name} {c.type}"]
                if c.primary_key:
                    bits.append("PRIMARY KEY")
                if c.references:
                    bits.append(f"REFERENCES {c.references}")
                if c.sample_values:
                    vals = ", ".join(repr(v) for v in c.sample_values)
                    bits.append(f"-- values: {vals}")
                lines.append(" ".join(bits))
            lines.append("")
        return "\n".join(lines).strip()

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "tables": [t.to_dict() for t in self.tables],
        }


_schema_lock = threading.Lock()
_schema_cache: SchemaSnapshot | None = None


def introspect_schema(force: bool = False) -> SchemaSnapshot:
    """Introspect the live database (cached for SCHEMA_CACHE_TTL_S)."""
    global _schema_cache
    s = get_settings()
    with _schema_lock:
        if (
            not force
            and _schema_cache
            and time.time() - _schema_cache.created_at < s.schema_cache_ttl_s
        ):
            return _schema_cache

        insp = inspect(exec_engine)
        schema = s.schema_name
        excluded = s.excluded_tables
        tables: list[TableInfo] = []

        # estimated row counts from planner stats (cheap)
        estimates: dict[str, int] = {}
        with exec_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.relname, c.reltuples::bigint FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=:s AND c.relkind='r'"
                ),
                {"s": schema},
            )
            estimates = {r[0]: max(int(r[1]), 0) for r in rows}

        for tname in sorted(insp.get_table_names(schema=schema)):
            if tname.lower() in excluded:
                continue
            pk = set(insp.get_pk_constraint(tname, schema=schema).get("constrained_columns") or [])
            fks: dict[str, str] = {}
            for fk in insp.get_foreign_keys(tname, schema=schema):
                for col, rcol in zip(fk["constrained_columns"], fk["referred_columns"]):
                    fks[col] = f"{fk['referred_table']}.{rcol}"
            cols = [
                ColumnInfo(
                    name=c["name"],
                    type=str(c["type"]),
                    nullable=bool(c.get("nullable", True)),
                    primary_key=c["name"] in pk,
                    references=fks.get(c["name"]),
                )
                for c in insp.get_columns(tname, schema=schema)
            ]
            tables.append(TableInfo(tname, cols, estimates.get(tname)))

        if s.schema_sample_enums:
            _attach_enum_samples(tables, schema)

        _schema_cache = SchemaSnapshot(tables, time.time())
        return _schema_cache


def _attach_enum_samples(tables: list[TableInfo], schema: str) -> None:
    """For short text columns with few distinct values, list them for the LLM."""
    with exec_engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        for t in tables:
            for c in t.columns:
                tl = c.type.lower()
                if c.primary_key or c.references:
                    continue
                if not any(k in tl for k in ("char", "text", "varchar")):
                    continue
                try:
                    q = text(
                        f'SELECT DISTINCT "{c.name}" FROM "{schema}"."{t.name}" '
                        f'WHERE "{c.name}" IS NOT NULL LIMIT 13'
                    )
                    vals = [str(r[0]) for r in conn.execute(q)]
                except Exception:
                    conn.rollback()
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    continue
                if 0 < len(vals) <= 12 and all(len(v) <= 40 for v in vals):
                    c.sample_values = sorted(vals)
        conn.rollback()


# --------------------------------------------------------------------------- #
# Safe execution
# --------------------------------------------------------------------------- #
@dataclass
class ExecResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return "<binary>"
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def execute_readonly(sql: str) -> ExecResult:
    """Run `sql` in a READ ONLY transaction with a statement timeout and row cap.
    The transaction is *always* rolled back (second safety layer after the AST check)."""
    s = get_settings()
    started = time.perf_counter()
    with exec_engine.connect() as conn:
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {int(s.statement_timeout_ms)}"))
            result = conn.execute(text(sql))
            columns = list(result.keys())
            fetched = result.fetchmany(s.max_rows + 1)
        finally:
            conn.rollback()
    truncated = len(fetched) > s.max_rows
    fetched = fetched[: s.max_rows]
    rows = [[_jsonable(v) for v in r] for r in fetched]
    return ExecResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Query history (schema `app`, invisible to the LLM and to the read-only role)
# --------------------------------------------------------------------------- #
def init_app_tables() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app.query_history (
                    id          BIGSERIAL PRIMARY KEY,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    question    TEXT NOT NULL,
                    sql         TEXT,
                    ok          BOOLEAN NOT NULL,
                    error       TEXT,
                    row_count   INTEGER,
                    attempts    INTEGER NOT NULL DEFAULT 1,
                    llm_calls   INTEGER NOT NULL DEFAULT 0,
                    latency_ms  INTEGER NOT NULL DEFAULT 0,
                    cached      BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app.uploaded_tables (
                    table_name        TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    row_count         INTEGER NOT NULL,
                    column_count      INTEGER NOT NULL,
                    uploaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )


def save_history(
    *,
    question: str,
    sql: str | None,
    ok: bool,
    error: str | None,
    row_count: int | None,
    attempts: int,
    llm_calls: int,
    latency_ms: int,
    cached: bool = False,
) -> int | None:
    try:
        with engine.begin() as conn:
            r = conn.execute(
                text(
                    "INSERT INTO app.query_history "
                    "(question, sql, ok, error, row_count, attempts, llm_calls, latency_ms, cached) "
                    "VALUES (:question,:sql,:ok,:error,:row_count,:attempts,:llm_calls,:latency_ms,:cached) "
                    "RETURNING id"
                ),
                dict(
                    question=question, sql=sql, ok=ok, error=error, row_count=row_count,
                    attempts=attempts, llm_calls=llm_calls, latency_ms=latency_ms, cached=cached,
                ),
            )
            return r.scalar_one()
    except Exception:
        return None


def list_history(limit: int = 50) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, created_at, question, sql, ok, error, row_count, attempts, "
                "llm_calls, latency_ms, cached FROM app.query_history "
                "ORDER BY id DESC LIMIT :n"
            ),
            {"n": limit},
        ).mappings()
        return [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]


def clear_history() -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE app.query_history"))


def history_stats() -> dict:
    with engine.connect() as conn:
        r = conn.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE ok) AS succeeded, "
                "coalesce(avg(latency_ms),0) AS avg_latency_ms, "
                "coalesce(avg(llm_calls),0) AS avg_llm_calls, "
                "count(*) FILTER (WHERE attempts > 1) AS needed_retry, "
                "count(*) FILTER (WHERE cached) AS cache_hits "
                "FROM app.query_history"
            )
        ).mappings().one()
    d = {k: _jsonable(v) for k, v in dict(r).items()}
    d["avg_latency_ms"] = round(d["avg_latency_ms"])
    d["avg_llm_calls"] = round(d["avg_llm_calls"], 2)
    return d
