"""Execution-match benchmark.   Run from ./backend:   python -m eval.run_eval

A generated query counts as correct when its result set equals the gold query's
result set (compared as unordered multisets of rows, floats rounded to 2 dp).
Also checks that destructive SQL is blocked by the validator.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from app import db
from app.pipeline import run_pipeline
from app.validator import validate_sql


def _norm(rows):
    def cell(v):
        return round(v, 2) if isinstance(v, float) else v
    return Counter(tuple(cell(v) for v in r) for r in rows)


def main() -> None:
    bench = json.loads((Path(__file__).parent / "benchmark.json").read_text())
    passed = 0
    llm_calls = 0
    t0 = time.time()

    for i, case in enumerate(bench, 1):
        gold = db.execute_readonly(case["gold"])
        final = None
        for ev in run_pipeline(case["question"]):
            if ev["type"] in ("result", "error"):
                final = ev
        ok = bool(final and final["type"] == "result"
                  and _norm(final["data"]["rows"]) == _norm(gold.rows))
        if final:
            llm_calls += (final.get("data") or final).get("llm_calls", 0)
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {i:>2}. {case['question']}")

    print(f"\nExecution accuracy: {passed}/{len(bench)} = {passed / len(bench):.0%}"
          f"   ({llm_calls} LLM calls, {time.time() - t0:.1f}s)")

    schema = db.introspect_schema()
    attacks = [
        "DROP TABLE customers", "DELETE FROM orders", "SELECT 1; DROP TABLE orders",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "SELECT pg_sleep(60)", "SELECT * FROM pg_catalog.pg_shadow",
    ]
    blocked = sum(not validate_sql(a, schema).ok for a in attacks)
    print(f"Safety: blocked {blocked}/{len(attacks)} malicious statements")


if __name__ == "__main__":
    main()
