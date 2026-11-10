"""Upload a CSV / Excel file from the UI and load it into PostgreSQL as a table.

Safety rules
  * Only .csv .tsv .txt .xlsx .xlsm files, size- and row-limited.
  * Table and column names are sanitised to plain lowercase identifiers.
  * Tables that already exist and were NOT created by an upload (e.g. the demo tables or the
    user's own database tables) can never be overwritten or deleted through this feature.
  * Uploaded tables are tracked in app.uploaded_tables; only those can be replaced / removed.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.types import BigInteger, Boolean, Date, DateTime, Float, Text

from . import db
from .config import get_settings

ALLOWED_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}
MAX_COLS = 200
NA_VALUES = ["-", "--"]

RESERVED = {
    "all", "analyse", "analyze", "and", "any", "as", "asc", "case", "cast", "check", "column",
    "constraint", "create", "current_date", "current_time", "current_user", "default", "desc",
    "distinct", "do", "else", "end", "false", "for", "from", "grant", "group", "having", "in",
    "into", "is", "join", "like", "limit", "not", "null", "offset", "on", "or", "order",
    "primary", "references", "select", "table", "then", "to", "true", "union", "unique", "user",
    "using", "when", "where", "with",
}
PROTECTED_NAMES = {"app", "public"}

_DATE_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}([ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?)?$")
_ISO_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}")
# plain, Western-grouped (1,234,567.89) or Indian-grouped (12,34,567.89) numbers
_NUM_RE = re.compile(r"^[+-]?(?:(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?|\.\d+)$")


class UploadError(ValueError):
    def __init__(self, message: str, code: str = "invalid", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- #
# Name cleaning
# --------------------------------------------------------------------------- #
def _slug(raw: str) -> str:
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)  # camelCase -> camel_Case
    ascii_ = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_.lower()).strip("_")


def clean_columns(raw_cols) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i, c in enumerate(raw_cols):
        raw = str(c)
        base = "" if raw.startswith("Unnamed:") else _slug(raw)
        if not base:
            base = f"column_{i + 1}"
        if base[0].isdigit():
            base = "c_" + base
        if base in RESERVED:
            base += "_"
        base = base[:55]
        name, n = base, 2
        while name in seen:
            name = f"{base}_{n}"
            n += 1
        seen.add(name)
        out.append(name)
    return out


def clean_table_name(raw: str) -> str:
    name = _slug(raw) or "my_data"
    if name[0].isdigit():
        name = "t_" + name
    if name.startswith("pg_"):
        name = "t_" + name
    if name in RESERVED or name in PROTECTED_NAMES:
        name += "_data"
    return name[:55]


# --------------------------------------------------------------------------- #
# Reading files
# --------------------------------------------------------------------------- #
def _check_row_widths(sample: str, sep: str) -> None:
    """Refuse files where a row has more values than the header (stray delimiters) instead of
    letting pandas silently drop or shift them. Trailing empty fields are fine."""
    rows = csv.reader(io.StringIO(sample), delimiter=sep)
    header = next(rows, [])
    width = len(header)
    while width and not header[width - 1].strip():
        width -= 1
    for line_no, row in enumerate(rows, start=2):
        n = len(row)
        while n and not row[n - 1].strip():
            n -= 1
        if n > width:
            raise UploadError(
                f"Line {line_no} has {n} values but the header only has {width}. "
                "Please check the file for stray commas or unquoted text containing commas."
            )


def _read_csv(data: bytes, ext: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        sample = data[:20000].decode(enc, errors="ignore")
        if ext == ".tsv":
            sep = "\t"
        else:
            try:  # only real delimiters are candidates (never letters from the data)
                sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                sep = ","
        _check_row_widths(sample, sep)
        try:
            # dtype=str: pandas must not guess types (it would turn 00501 into 501);
            # _tidy_column() infers numbers / dates / booleans itself, safely.
            # index_col=False: without it, a trailing comma on every line makes pandas silently
            # shift all values one column to the left.
            return pd.read_csv(
                io.BytesIO(data), sep=sep, encoding=enc, na_values=NA_VALUES, dtype=str, index_col=False
            )
        except UnicodeDecodeError:
            continue  # try the next encoding
        except pd.errors.ParserError as e:
            raise UploadError(f"Could not read this CSV file ({str(e)[:140]}).")
    raise UploadError("Could not read this CSV file (unknown text encoding).")


def _read(ext: str, data: bytes) -> list[tuple[str | None, pd.DataFrame]]:
    try:
        if ext in (".xlsx", ".xlsm"):
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, engine="openpyxl", na_values=NA_VALUES)
            return [(str(n), df) for n, df in sheets.items()]
        return [(None, _read_csv(data, ext))]
    except UploadError:
        raise
    except pd.errors.EmptyDataError:
        raise UploadError("The file has no data in it.")
    except Exception as e:  # noqa: BLE001
        raise UploadError(f"Could not read this file: {str(e)[:160]}")


# --------------------------------------------------------------------------- #
# Type inference
# --------------------------------------------------------------------------- #
def _tidy_column(s: pd.Series, state: dict):
    """Return (clean series, SQLAlchemy type)."""
    # pandas 2 stores text as `object`, pandas 3 as a dedicated string dtype: handle both
    if pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
        s = s.astype(object)
        s = s.map(lambda v: v.strip() if isinstance(v, str) else v)
        s = s.map(lambda v: None if (isinstance(v, str) and v == "") else v)
        nn = s.dropna()
        if len(nn):
            if nn.map(lambda v: isinstance(v, (bool, np.bool_))).all():
                return s.map(lambda v: None if pd.isna(v) else bool(v)), Boolean()
            if nn.map(lambda v: hasattr(v, "year") and hasattr(v, "month")).all():
                s = pd.to_datetime(s, errors="coerce")  # Excel date objects
            elif nn.map(lambda v: isinstance(v, str)).all():
                if nn.str.lower().isin(["true", "false"]).all():
                    return s.map(lambda v: None if pd.isna(v) else v.lower() == "true"), Boolean()
                # numbers written with symbols: "₹1,200", "12%"
                stripped = nn.str.replace(r"[\s₹$€£%]", "", regex=True)
                # ids like 007 / 00501 and very long digit strings stay text
                keeps_zeros = nn.str.match(r"^[+-]?0\d").any() or nn.str.match(r"^\d{16,}$").any()
                # commas only count as thousands separators when the grouping is valid
                # (1,200.50 or 12,34,567); "2,5" (decimal comma) stays text instead of becoming 25
                if not keeps_zeros and stripped.str.match(_NUM_RE).all():
                    s = pd.to_numeric(s.str.replace(r"[,\s₹$€£%]", "", regex=True), errors="coerce")
                elif nn.str.match(_DATE_RE).all():
                    iso = bool(nn.str.match(_ISO_RE).all())
                    parsed = pd.to_datetime(s, errors="coerce", dayfirst=not iso, format="mixed")
                    if parsed[s.notna()].notna().all():
                        if not iso:
                            state["dayfirst"] = True
                        s = parsed

    if pd.api.types.is_bool_dtype(s):
        return s, Boolean()
    if pd.api.types.is_integer_dtype(s):
        return s, BigInteger()
    if pd.api.types.is_float_dtype(s):
        s = s.replace([np.inf, -np.inf], np.nan)
        nn = s.dropna()
        if len(nn) and (nn % 1 == 0).all() and nn.abs().max() < 9e15:
            return s.astype("Int64"), BigInteger()
        return s, Float(53)
    if pd.api.types.is_datetime64_any_dtype(s):
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_localize(None)
        nn = s.dropna()
        if len(nn) and (nn == nn.dt.normalize()).all():
            return s.map(lambda v: None if pd.isna(v) else v.date()), Date()
        return s.map(lambda v: None if pd.isna(v) else v.to_pydatetime()), DateTime()
    return s.map(lambda v: None if pd.isna(v) else str(v)), Text()


def prepare_frame(df: pd.DataFrame, state: dict | None = None):
    """Clean one DataFrame. Returns (frame, {col: sqltype}, notes)."""
    state = state if state is not None else {}
    notes: list[str] = []
    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if df.empty:
        return df, {}, notes
    old = [str(c) for c in df.columns]
    new = clean_columns(old)
    renamed = sum(1 for a, b in zip(old, new) if a != b)
    df.columns = new
    cols, types = {}, {}
    for c in df.columns:
        cols[c], types[c] = _tidy_column(df[c], state)
    if renamed:
        notes.append(f"{renamed} column name{'s' if renamed != 1 else ''} tidied (lowercase, underscores).")
    return pd.DataFrame(cols), types, notes


# --------------------------------------------------------------------------- #
# Registry + loading
# --------------------------------------------------------------------------- #
def uploaded_tables() -> list[dict]:
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, original_filename, row_count, column_count, uploaded_at "
                "FROM app.uploaded_tables ORDER BY uploaded_at DESC"
            )
        ).mappings()
        return [{**dict(r), "uploaded_at": r["uploaded_at"].isoformat()} for r in rows]


def uploaded_names() -> set[str]:
    try:
        return {u["table_name"] for u in uploaded_tables()}
    except Exception:
        return set()


def _existing_tables(conn, schema: str) -> set[str]:
    rows = conn.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"), {"s": schema}
    )
    return {r[0] for r in rows}


def handle_upload(filename: str, data: bytes, table_name: str | None, replace: bool) -> dict:
    s = get_settings()
    schema = s.schema_name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise UploadError("Please upload a .csv, .tsv or .xlsx file.", "bad_type")
    if not data:
        raise UploadError("The file is empty.", "empty")
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise UploadError(f"That file is larger than {s.max_upload_mb} MB.", "too_big", 413)

    frames = _read(ext, data)
    base = clean_table_name(table_name or Path(filename).stem)

    plan: list[tuple[str, pd.DataFrame]] = []
    used: set[str] = set()
    for label, df in frames:
        if df.dropna(how="all").empty:
            continue
        name = base if len(frames) == 1 else clean_table_name(f"{base}_{label}")
        n = 2
        while name in used:
            name = f"{name[:50]}_{n}"
            n += 1
        used.add(name)
        plan.append((name, df))
    if not plan:
        raise UploadError("No data rows were found in this file.", "empty")

    with db.engine.connect() as conn:
        existing = _existing_tables(conn, schema)
    registered = uploaded_names()
    protected = [n for n, _ in plan if n in existing and n not in registered]
    if protected:
        raise UploadError(
            f'A table named "{protected[0]}" already exists in your database (not from an upload), '
            "so it can't be overwritten. Please choose a different table name.",
            "exists_protected", 409,
        )
    clash = [n for n, _ in plan if n in registered]
    if clash and not replace:
        raise UploadError(
            f'You already uploaded a table named "{clash[0]}". Replace it with this file?',
            "exists_uploaded", 409,
        )

    state: dict = {}
    prepared = []
    for name, df in plan:
        if len(df) > s.max_upload_rows:
            raise UploadError(f"'{name}' has {len(df):,} rows; the limit is {s.max_upload_rows:,}.", "too_many_rows", 413)
        if df.shape[1] > MAX_COLS:
            raise UploadError(f"'{name}' has {df.shape[1]} columns; the limit is {MAX_COLS}.", "too_many_cols", 413)
        frame, types, notes = prepare_frame(df, state)
        if frame.empty:
            continue
        prepared.append((name, frame, types, notes))
    if not prepared:
        raise UploadError("No data rows were found in this file.", "empty")

    results = []
    with db.engine.begin() as conn:  # one transaction: all tables or none
        role = s.readonly_role
        has_role = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
        for name, frame, types, notes in prepared:
            was = name in existing
            frame.to_sql(
                name, conn, schema=schema, if_exists="replace" if was else "fail",
                index=False, chunksize=1000, method="multi", dtype=types,
            )
            conn.execute(text(f'ANALYZE "{schema}"."{name}"'))
            if has_role:
                conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
                conn.execute(text(f'GRANT SELECT ON "{schema}"."{name}" TO "{role}"'))
            conn.execute(
                text(
                    "INSERT INTO app.uploaded_tables (table_name, original_filename, row_count, column_count) "
                    "VALUES (:t, :f, :r, :c) ON CONFLICT (table_name) DO UPDATE SET "
                    "original_filename = EXCLUDED.original_filename, row_count = EXCLUDED.row_count, "
                    "column_count = EXCLUDED.column_count, uploaded_at = now()"
                ),
                {"t": name, "f": filename[:200], "r": len(frame), "c": frame.shape[1]},
            )
            results.append({
                "table": name, "rows": len(frame), "replaced": was,
                "columns": [{"name": c, "type": type(types[c]).__name__.lower()} for c in frame.columns],
                "notes": notes,
            })

    if state.get("dayfirst"):
        results[0]["notes"].append("Dates like 03/04/2024 were read as day/month/year.")

    snap = db.introspect_schema(force=True)
    suggestions: list[str] = []
    for r in results:
        t = next((t for t in snap.tables if t.name == r["table"]), None)
        if t:
            suggestions += suggestions_for(t)[:4]
    return {"tables": results, "suggestions": suggestions[:6]}


def delete_table(name: str) -> None:
    if name not in uploaded_names():
        raise UploadError(
            "Only tables created through Upload can be removed here.", "not_uploaded", 403
        )
    schema = get_settings().schema_name
    with db.engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{name}"'))
        conn.execute(text("DELETE FROM app.uploaded_tables WHERE table_name = :t"), {"t": name})
    db.introspect_schema(force=True)


# --------------------------------------------------------------------------- #
# Suggested questions (no LLM needed)
# --------------------------------------------------------------------------- #
def _kind(c) -> str:
    ty = c.type.lower()
    if "bool" in ty:
        return "bool"
    if any(k in ty for k in ("date", "time")):
        return "date"
    if any(k in ty for k in ("int", "numeric", "double", "float", "real", "decimal")):
        return "num"
    return "text"


def suggestions_for(t) -> list[str]:
    human = lambda s: s.replace("_", " ")  # noqa: E731
    nums = [c for c in t.columns if _kind(c) == "num" and c.name != "id" and not c.name.endswith("_id")]
    dates = [c for c in t.columns if _kind(c) == "date"]
    texts = [c for c in t.columns if _kind(c) == "text"]
    # best grouping column = text column with the fewest distinct values (but not unique per row)
    cats = sorted(
        (c for c in texts if c.sample_values and (not t.row_estimate or len(c.sample_values) < t.row_estimate)),
        key=lambda c: len(c.sample_values),
    ) or texts[:1]

    q = [f"How many rows are in {t.name}?", f"Show the first 10 rows of {t.name}"]
    if cats and nums:
        q.append(f"What is the average {human(nums[0].name)} for each {human(cats[0].name)} in {t.name}?")
        q.append(f"Which {human(cats[0].name)} has the highest total {human(nums[0].name)} in {t.name}?")
    if dates and nums:
        q.append(f"What is the total {human(nums[0].name)} per month in {t.name}?")
    if cats:
        q.append(f"How many records are there for each {human(cats[0].name)} in {t.name}?")
    return q[:6]
