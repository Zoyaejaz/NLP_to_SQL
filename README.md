# NLP2SQL Pro

Ask questions about a **PostgreSQL** database in plain English. A **FastAPI** backend turns them into SQL
with an LLM (Groq via LangChain), validates the SQL structurally, runs it read-only, self-corrects on errors,
and explains the answer. A custom web UI (no Streamlit) streams every pipeline stage live.

```
question ─▶ Intent (LLM 1) ─▶ SQL generation on live schema (LLM 2)
                                   │
                    ┌──────────────▼──────────────┐
                    │ sqlglot AST validation       │── fail ──┐
                    │ READ ONLY txn + timeout      │── error ─┤  retry with the error
                    └──────────────┬──────────────┘           │  fed back to the LLM
                                   │ ok                       ◀┘  (up to MAX_SQL_RETRIES)
                                   ▼
                  Plain-English summary (LLM) ─▶ table · chart · CSV
```

## Quick start (Docker – recommended)

```bash
cp .env.example .env          # put your GROQ_API_KEY in it (free at console.groq.com)
docker compose up --build
# open http://localhost:8000
```

This starts PostgreSQL 16, creates a **SELECT-only** role, seeds a demo e-commerce + HR dataset, and serves the API + UI.

## Quick start (local)

```bash
# 1. a Postgres you can reach (or: docker compose up db)
cp .env.example .env          # set GROQ_API_KEY and DATABASE_URL
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.seed            # demo data (add --reset to recreate)
uvicorn app.main:app --reload
# open http://localhost:8000   (API docs: /docs)
```

## Upload your own data (no SQL, no database tools)

Click **Upload your data** in the sidebar (or the link on the start screen), drop a **CSV, TSV or Excel (.xlsx)**
file, optionally rename the table, and press Upload. The backend cleans the file, loads it into PostgreSQL as a
new table and immediately offers suggested questions. Then ask away.

* Excel files with several sheets become one table per sheet (`<file>_<sheet>`).
* Column names are tidied (`Order Date` -> `order_date`); numbers like `₹1,20,000` or `12%`, `dd/mm/yyyy` dates,
  booleans and ZIP codes with leading zeros are handled; a decimal comma (`2,5`) is kept as text rather than guessed.
* Uploaded tables carry an **UPLOADED** tag and can be removed with the ✕ button. Tables that were *not* created by an
  upload (the demo tables, or your own database's tables) can never be overwritten or deleted from the UI.
* Re-uploading a file with the same table name asks before replacing it.
* Limits: 25 MB and 500,000 rows per file (`MAX_UPLOAD_MB`, `MAX_UPLOAD_ROWS`).
* Privacy: with `SCHEMA_SAMPLE_ENUMS=true` the app sends a few distinct values of low-cardinality text columns, and with
  `SUMMARIZE_RESULTS=true` up to 25 result rows, to the LLM provider. Turn both off for sensitive data.

## Use it on your own database

Set `DATABASE_URL` (and ideally `READONLY_DATABASE_URL`, a role with only `SELECT`) and restart. Schema
introspection is live and cached; nothing is hardcoded. Hide tables with `EXCLUDE_TABLES=a,b`.
Set `SCHEMA_SAMPLE_ENUMS=false` if you don't want low-cardinality text values (e.g. `status`) sent to the LLM.
The app stores its own history in a separate `app` schema, which the LLM never sees.

## Features

| Area | What it does |
|---|---|
| Schema | Introspected live from PostgreSQL (tables, types, PK/FK, row estimates, enum-like values), cached with TTL |
| Safety | 3 independent layers: sqlglot AST walk (single SELECT only; blocks DDL/DML, `SELECT INTO`, `FOR UPDATE`, writes hidden in CTEs, `pg_sleep`, `pg_read_file`, system schemas, unknown tables) → `READ ONLY` transaction always rolled back + `statement_timeout` + row cap → optional SELECT-only DB role |
| Self-correction | Parser / Postgres errors are fed back to the LLM; shown in the UI under "Self-correction" |
| Upload | CSV / TSV / Excel upload from the browser straight into PostgreSQL, with cleaning, type inference and protection of existing tables |
| Streaming UI | Live stage stepper, syntax-highlighted SQL, **Edit & run** (re-validated, no LLM), sortable table, auto bar/line chart, CSV export, dark/light theme |
| Follow-ups | Multi-turn context ("now only for Pune") – toggle in the header |
| Caching | TTL cache for repeated context-free questions |
| Observability | Per-query latency, LLM call count, retries and cache hits in History + Stats tabs |
| Evaluation | `python -m eval.run_eval` – execution-match accuracy on `eval/benchmark.json` + safety check |

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | DB + LLM status |
| GET | `/api/schema` · POST `/api/schema/refresh` | Introspected schema |
| POST | `/api/query` | Full pipeline, JSON result |
| POST | `/api/query/stream` | Same, as NDJSON progress events |
| POST | `/api/execute` | Run user-edited SQL (validated, read-only) |
| POST | `/api/upload` | Upload a CSV / Excel file (multipart: `file`, optional `table_name`, `replace`) |
| GET | `/api/uploads` | Tables created through upload |
| DELETE | `/api/tables/{name}` | Remove an uploaded table (original tables are protected) |
| GET/DELETE | `/api/history` | Query history |
| GET | `/api/stats` | Aggregate latency / success / retry stats |

```bash
curl -s localhost:8000/api/query -H 'content-type: application/json' \
     -d '{"question":"average salary per department"}' | jq '.sql, .summary'
```

## Project layout

```
nlp2sql-pro/
├── backend/
│   ├── app/
│   │   ├── config.py      env-driven settings
│   │   ├── db.py          engines, introspection, read-only execution, history
│   │   ├── validator.py   sqlglot AST validation
│   │   ├── prompts.py     intent / SQL / retry / summary prompts
│   │   ├── llm.py         Groq client (LangChain)
│   │   ├── pipeline.py    orchestration + retry loop (generator of events)
│   │   ├── uploads.py     CSV/Excel -> PostgreSQL (cleaning, type inference, safety rules)
│   │   ├── main.py        FastAPI routes + static frontend
│   │   └── seed.py        demo dataset
│   ├── eval/              benchmark + runner
│   ├── tests/             validator tests (pytest)
│   └── requirements.txt, Dockerfile
├── frontend/              index.html, style.css, app.js (no build step)
├── db/init/01_roles.sql   SELECT-only role
├── docker-compose.yml
└── .env.example
```

## Tests

```bash
cd backend && pip install pytest && pytest -q     # validator + upload cleaning – no DB or API key needed
python -m eval.run_eval                           # needs DB + GROQ_API_KEY
```

## Production notes

Put it behind auth (e.g. an API-key or OAuth dependency on the routes), add rate limiting, restrict
`CORS_ORIGINS`, and always use a dedicated read-only database role.
