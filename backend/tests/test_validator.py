"""Validator tests – no database or LLM needed.   Run: pytest -q   (from ./backend)"""
import pytest

from app.db import ColumnInfo, SchemaSnapshot, TableInfo
from app.validator import validate_sql

SCHEMA = SchemaSnapshot(
    tables=[
        TableInfo("employees", [ColumnInfo("employee_id", "integer", False, True),
                                ColumnInfo("salary", "numeric", False)]),
        TableInfo("departments", [ColumnInfo("department_id", "integer", False, True)]),
    ],
    created_at=0,
)

GOOD = [
    "SELECT * FROM employees",
    "SELECT department_id, count(*) FROM employees GROUP BY 1 ORDER BY 2 DESC LIMIT 5;",
    "WITH t AS (SELECT salary FROM employees) SELECT avg(salary) FROM t",
    "SELECT salary FROM employees UNION SELECT department_id FROM departments",
    "SELECT e.salary FROM employees e JOIN departments d ON d.department_id = e.employee_id",
]

BAD = [
    "DROP TABLE employees",
    "DELETE FROM employees",
    "UPDATE employees SET salary = 0",
    "INSERT INTO employees VALUES (1, 2)",
    "SELECT * FROM employees; DROP TABLE employees",          # stacked
    "SELECT * INTO backup FROM employees",                     # SELECT INTO
    "SELECT * FROM employees FOR UPDATE",                      # row locks
    "WITH x AS (DELETE FROM employees RETURNING *) SELECT * FROM x",  # write hidden in a CTE
    "SELECT pg_sleep(30)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM pg_catalog.pg_user",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM app.query_history",
    "SELECT * FROM secrets",                                   # unknown table
    "SET ROLE postgres",
    "COPY employees TO '/tmp/x'",
    "TRUNCATE employees",
    "",
    "SELEC * FRM",
]


@pytest.mark.parametrize("sql", GOOD)
def test_allows_safe_selects(sql):
    r = validate_sql(sql, SCHEMA)
    assert r.ok, r.error


@pytest.mark.parametrize("sql", BAD)
def test_blocks_unsafe_or_invalid(sql):
    r = validate_sql(sql, SCHEMA)
    assert not r.ok
    assert r.error
