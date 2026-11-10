-- Dedicated SELECT-only role used to execute LLM-generated SQL (third safety layer).
-- Runs once, on first container start. The seed script grants it SELECT on the demo tables.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nlp_readonly') THEN
    CREATE ROLE nlp_readonly LOGIN PASSWORD 'readonly';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE nlp2sql TO nlp_readonly;
ALTER ROLE nlp_readonly SET default_transaction_read_only = on;
ALTER ROLE nlp_readonly SET statement_timeout = '10s';
-- future tables created by the owner are readable too
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nlp_readonly;
