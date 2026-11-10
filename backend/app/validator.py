"""AST-based SQL validation with sqlglot (PostgreSQL dialect).

Independent of anything the LLM claims: the query is parsed and the *entire tree*
is walked. Only a single read-only SELECT (optionally with CTEs / UNIONs) passes.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .db import SchemaSnapshot

DIALECT = "postgres"

# Node types that must never appear anywhere in the tree.
_FORBIDDEN_NAMES = [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Merge",
    "TruncateTable", "Command", "Copy", "Set", "Grant", "Revoke", "Into", "Lock",
    "Transaction", "Commit", "Rollback", "Use", "Pragma", "Analyze", "Kill",
    "Declare", "Execute",
]
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

# Functions that read files, sleep, open connections or change server state.
FORBIDDEN_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "lo_open", "lo_unlink",
    "dblink", "dblink_exec", "dblink_connect", "set_config", "pg_terminate_backend",
    "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile", "copy_from",
    "current_setting", "txid_current", "pg_advisory_lock", "pg_notify",
}

SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast", "app"}

ALLOWED_ROOTS = tuple(
    getattr(exp, n) for n in ("Select", "Union", "Subquery", "Intersect", "Except") if hasattr(exp, n)
)


@dataclass
class ValidationResult:
    ok: bool
    sql: str | None = None          # normalised SQL when ok
    error: str | None = None        # message fed back to the LLM when not ok
    tables: list[str] | None = None


def validate_sql(sql: str, schema: SchemaSnapshot | None = None) -> ValidationResult:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return ValidationResult(False, error="Empty query.")

    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (ParseError, TokenError) as e:
        return ValidationResult(False, error=f"SQL syntax error: {str(e)[:300]}")

    if len(statements) != 1:
        return ValidationResult(
            False, error="Exactly one SQL statement is allowed (no stacked queries)."
        )
    tree = statements[0]

    if not isinstance(tree, ALLOWED_ROOTS):
        return ValidationResult(
            False,
            error=f"Only SELECT queries are allowed; got {type(tree).__name__.upper()}.",
        )

    # Walk the ENTIRE tree: catches writes hidden in CTEs, subqueries, SELECT INTO, FOR UPDATE.
    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node
        if isinstance(node, FORBIDDEN_NODES):
            return ValidationResult(
                False, error=f"Forbidden operation in query: {type(node).__name__.upper()}."
            )
        if isinstance(node, exp.Func):
            name = (
                node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            ).lower()
            if name in FORBIDDEN_FUNCTIONS:
                return ValidationResult(False, error=f"Function '{name}' is not allowed.")

    # Table checks
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    used: list[str] = []
    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if not name or name in cte_names:
            continue
        if (t.db or "").lower() in SYSTEM_SCHEMAS:
            return ValidationResult(False, error=f"Access to schema '{t.db}' is not allowed.")
        if schema is not None and name not in schema.table_names:
            known = ", ".join(sorted(schema.table_names))
            return ValidationResult(
                False, error=f"Unknown table '{t.name}'. Available tables: {known}."
            )
        used.append(name)

    # Column sanity check for fully-qualified references (alias.col) is left to Postgres;
    # its error message is fed back to the LLM by the retry loop.
    normalised = tree.sql(dialect=DIALECT, pretty=True)
    return ValidationResult(True, sql=normalised, tables=sorted(set(used)))
