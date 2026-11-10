"""Orchestrates: intent -> SQL generation -> AST validation -> read-only execution
(with a self-correcting retry loop) -> summarisation. Implemented as a generator that
yields progress events so the UI can stream stages live."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

from cachetools import TTLCache
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from . import db, prompts
from .config import get_settings
from .llm import LLMNotConfigured, call_llm
from .validator import validate_sql

_FENCE = re.compile(r"```(?:sql|postgresql|pgsql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

_cache: TTLCache | None = None


def _get_cache() -> TTLCache:
    global _cache
    if _cache is None:
        _cache = TTLCache(maxsize=256, ttl=get_settings().query_cache_ttl_s)
    return _cache


def _extract_sql(text: str) -> str:
    """Pull SQL out of the model response (fenced block, else the raw text)."""
    m = _FENCE.search(text)
    return (m.group(1) if m else text).strip()


def _parse_intent(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"goal": text[:200], "answerable": True}


def _clean_db_error(e: Exception) -> str:
    """Short, LLM-friendly database error."""
    if isinstance(e, DBAPIError) and getattr(e, "orig", None) is not None:
        return str(e.orig).strip().splitlines()[0][:400]
    return str(e).strip().splitlines()[0][:400]


def run_pipeline(question: str, conversation: list[dict] | None = None) -> Iterator[dict[str, Any]]:
    s = get_settings()
    conversation = conversation or []
    started = time.perf_counter()
    llm_calls = 0
    attempts: list[dict] = []
    question = question.strip()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        schema = db.introspect_schema()
    except SQLAlchemyError as e:
        yield {"type": "error", "message": f"Could not read database schema: {_clean_db_error(e)}"}
        return
    schema_text = schema.to_prompt()

    # ---- cache (only for context-free questions) ---------------------------
    cache_key = (question.lower(), schema.fingerprint)
    if not conversation and cache_key in _get_cache():
        payload = dict(_get_cache()[cache_key])
        payload.update(cached=True, latency_ms=elapsed(), llm_calls=0)
        payload["history_id"] = db.save_history(
            question=question, sql=payload["sql"], ok=True, error=None,
            row_count=payload["row_count"], attempts=1, llm_calls=0,
            latency_ms=payload["latency_ms"], cached=True,
        )
        yield {"type": "stage", "stage": "cache", "status": "done"}
        yield {"type": "result", "data": payload}
        return

    try:
        # ---- 1. intent --------------------------------------------------------
        yield {"type": "stage", "stage": "intent", "status": "running"}
        raw_intent = call_llm(
            prompts.INTENT_SYSTEM, f"SCHEMA:\n{schema_text}\n\nQuestion: {question}"
        )
        llm_calls += 1
        intent = _parse_intent(raw_intent)
        yield {"type": "stage", "stage": "intent", "status": "done", "data": intent}

        if intent.get("answerable") is False:
            reason = intent.get("reason") or "The schema does not contain the data needed."
            yield from _fail(question, None, reason, attempts, llm_calls, elapsed())
            return

        # ---- 2. generate SQL ----------------------------------------------------
        yield {"type": "stage", "stage": "generate", "status": "running"}
        raw = call_llm(
            prompts.SQL_SYSTEM,
            prompts.sql_user(schema_text, question, json.dumps(intent), conversation),
        )
        llm_calls += 1
        yield {"type": "stage", "stage": "generate", "status": "done"}

        # ---- 3/4. validate + execute with self-correction ----------------------
        exec_result = None
        sql = ""
        last_error = ""
        for attempt in range(1, s.max_sql_retries + 2):
            if raw.upper().startswith("CANNOT_ANSWER"):
                reason = raw.split(":", 1)[-1].strip() or "Not answerable with this schema."
                yield from _fail(question, None, reason, attempts, llm_calls, elapsed())
                return

            sql = _extract_sql(raw)
            yield {"type": "stage", "stage": "validate", "status": "running", "attempt": attempt}
            v = validate_sql(sql, schema)
            if not v.ok:
                last_error = v.error or "Invalid SQL"
                attempts.append({"attempt": attempt, "sql": sql, "stage": "validation", "error": last_error})
                yield {"type": "attempt", **attempts[-1]}
            else:
                sql = v.sql or sql
                yield {"type": "stage", "stage": "validate", "status": "done"}
                yield {"type": "stage", "stage": "execute", "status": "running", "attempt": attempt}
                try:
                    exec_result = db.execute_readonly(sql)
                    yield {"type": "stage", "stage": "execute", "status": "done"}
                    break
                except (SQLAlchemyError, Exception) as e:  # noqa: BLE001
                    last_error = _clean_db_error(e)
                    attempts.append({"attempt": attempt, "sql": sql, "stage": "execution", "error": last_error})
                    yield {"type": "attempt", **attempts[-1]}

            if attempt > s.max_sql_retries:
                break
            yield {"type": "stage", "stage": "retry", "status": "running", "attempt": attempt}
            raw = call_llm(
                prompts.RETRY_SYSTEM, prompts.retry_user(schema_text, question, sql, last_error)
            )
            llm_calls += 1

        if exec_result is None:
            yield from _fail(
                question, sql, f"Could not produce a valid query after {len(attempts)} attempts. "
                f"Last error: {last_error}", attempts, llm_calls, elapsed(),
            )
            return

        # ---- 5. summary -----------------------------------------------------------
        summary = None
        if s.summarize_results:
            yield {"type": "stage", "stage": "summarize", "status": "running"}
            try:
                summary = call_llm(
                    prompts.SUMMARY_SYSTEM,
                    prompts.summary_user(
                        question, exec_result.columns, exec_result.rows[:25], exec_result.row_count
                    ),
                )
                llm_calls += 1
            except Exception:  # summary is best-effort
                summary = None
            yield {"type": "stage", "stage": "summarize", "status": "done"}

        payload = {
            "question": question,
            "intent": intent,
            "sql": sql,
            "attempts": attempts,
            "columns": exec_result.columns,
            "rows": exec_result.rows,
            "row_count": exec_result.row_count,
            "truncated": exec_result.truncated,
            "exec_ms": exec_result.elapsed_ms,
            "summary": summary,
            "cached": False,
            "llm_calls": llm_calls,
            "latency_ms": elapsed(),
        }
        payload["history_id"] = db.save_history(
            question=question, sql=sql, ok=True, error=None, row_count=exec_result.row_count,
            attempts=len(attempts) + 1, llm_calls=llm_calls, latency_ms=payload["latency_ms"],
        )
        if not conversation:
            _get_cache()[cache_key] = payload
        yield {"type": "result", "data": payload}

    except LLMNotConfigured as e:
        yield {"type": "error", "message": str(e)}
    except Exception as e:  # noqa: BLE001  (LLM/network failure etc.)
        db.save_history(
            question=question, sql=None, ok=False, error=str(e)[:500], row_count=None,
            attempts=len(attempts) or 1, llm_calls=llm_calls, latency_ms=elapsed(),
        )
        yield {"type": "error", "message": f"Pipeline error: {str(e)[:400]}"}


def _fail(question, sql, message, attempts, llm_calls, latency_ms) -> Iterator[dict]:
    db.save_history(
        question=question, sql=sql, ok=False, error=message[:500], row_count=None,
        attempts=max(len(attempts), 1), llm_calls=llm_calls, latency_ms=latency_ms,
    )
    yield {
        "type": "error", "message": message, "sql": sql, "attempts": attempts,
        "llm_calls": llm_calls,
    }


def run_manual_sql(sql: str) -> dict:
    """Execute user-edited SQL through the same validator + read-only executor (no LLM)."""
    schema = db.introspect_schema()
    v = validate_sql(sql, schema)
    if not v.ok:
        raise ValueError(v.error)
    try:
        r = db.execute_readonly(v.sql or sql)
    except Exception as e:  # noqa: BLE001
        raise ValueError(_clean_db_error(e)) from e
    return {
        "sql": v.sql, "columns": r.columns, "rows": r.rows, "row_count": r.row_count,
        "truncated": r.truncated, "exec_ms": r.elapsed_ms,
    }
