"""Environment-driven configuration."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(ROOT / ".env"), ".env"), extra="ignore"
    )

    # --- Database -----------------------------------------------------------
    # Admin/app connection: used for the history table and the seed script.
    database_url: str = "postgresql+psycopg2://nlp2sql:nlp2sql@localhost:5432/nlp2sql"
    # Optional: a dedicated SELECT-only role used to execute generated SQL.
    # Falls back to database_url (still protected by AST validation + READ ONLY txn).
    readonly_database_url: str | None = None
    schema_name: str = "public"
    statement_timeout_ms: int = 8000
    max_rows: int = 500
    readonly_role: str = "nlp_readonly"  # granted SELECT on every uploaded table

    # --- Uploads --------------------------------------------------------------
    max_upload_mb: int = 25
    max_upload_rows: int = 500_000

    # --- LLM (Groq via LangChain) -------------------------------------------
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    llm_temperature: float = 0.0
    max_sql_retries: int = 3
    summarize_results: bool = True

    # --- Schema context -----------------------------------------------------
    schema_cache_ttl_s: int = 300
    # Send low-cardinality text values (e.g. status = 'delivered') to the LLM.
    # Improves accuracy a lot; turn off if values are sensitive.
    schema_sample_enums: bool = True
    exclude_tables: str = ""  # comma separated

    # --- Server -------------------------------------------------------------
    cors_origins: str = "*"
    query_cache_ttl_s: int = 300

    @property
    def excluded_tables(self) -> set[str]:
        return {t.strip().lower() for t in self.exclude_tables.split(",") if t.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
