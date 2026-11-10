"""All prompt templates. Plain strings; filled with str.format-free helpers so that
braces in SQL / schema text never cause formatting errors."""

INTENT_SYSTEM = """You are a data analyst who reads a natural-language question about a \
relational database and extracts its intent. Respond with ONLY a JSON object:
{
  "goal": "one sentence restating what the user wants",
  "tables": ["tables likely needed"],
  "metrics": ["aggregations or measures, if any"],
  "filters": ["conditions mentioned"],
  "grouping": ["dimensions to group by, if any"],
  "ordering": "ordering / top-N requirement or null",
  "answerable": true,
  "reason": "if not answerable from this schema, why"
}
Set "answerable" to false only if the schema clearly lacks the data needed."""

SQL_SYSTEM = """You are an expert PostgreSQL engineer. Write ONE read-only PostgreSQL \
SELECT query that answers the user's question using ONLY the tables and columns in the schema.

Rules:
- Output only the query inside a ```sql fenced block. No explanation.
- SELECT statements only (CTEs and window functions are fine). Never modify data.
- Use exact table and column names from the schema. Qualify columns when joining.
- Use the foreign keys shown in the schema for joins.
- Prefer readable aliases for computed columns (e.g. AS total_revenue).
- Round money / averages to 2 decimals with ROUND(x::numeric, 2).
- When the user asks for "top N" or "highest/lowest", add ORDER BY and LIMIT.
- If a text column lists possible values in the schema, use those exact values.
- If the question cannot be answered from the schema, output exactly:
  CANNOT_ANSWER: <short reason>"""


def sql_user(schema: str, question: str, intent: str, conversation: list[dict]) -> str:
    ctx = ""
    if conversation:
        turns = "\n".join(
            f"Q: {t.get('question','')}\nSQL: {t.get('sql','')}" for t in conversation[-3:]
        )
        ctx = (
            "Previous turns in this conversation (the new question may refer to them, "
            "e.g. 'now filter that to 2024'):\n" + turns + "\n\n"
        )
    return (
        f"DATABASE SCHEMA (PostgreSQL):\n{schema}\n\n{ctx}"
        f"Extracted intent:\n{intent}\n\nQuestion: {question}"
    )


RETRY_SYSTEM = SQL_SYSTEM + """

Your previous attempt failed. Fix it. Study the error carefully, do not repeat the same \
mistake, and return a corrected query in a ```sql fenced block."""


def retry_user(schema: str, question: str, failed_sql: str, error: str) -> str:
    return (
        f"DATABASE SCHEMA (PostgreSQL):\n{schema}\n\nQuestion: {question}\n\n"
        f"Failed query:\n```sql\n{failed_sql}\n```\n\nError:\n{error}\n\n"
        "Return the corrected query."
    )


SUMMARY_SYSTEM = """You explain SQL query results to a business user in plain English. \
Answer the question directly in 1-3 sentences using the actual numbers. Do not mention SQL, \
tables or columns. If the result is empty, say no matching data was found. Be precise; \
never invent numbers that are not in the result."""


def summary_user(question: str, columns: list[str], rows: list[list], total: int) -> str:
    head = " | ".join(columns)
    body = "\n".join(" | ".join(str(c) for c in r) for r in rows)
    note = f"\n(showing {len(rows)} of {total} rows)" if total > len(rows) else ""
    return f"Question: {question}\n\nResult:\n{head}\n{body}{note}"
