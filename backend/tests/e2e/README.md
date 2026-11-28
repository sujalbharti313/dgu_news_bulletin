# Phase 4.2 end-to-end scenario tests

Each file covers one scenario of the Phase 4.2 test plan (A ingestion ... M public API). Every test carries a test
ID (for example `E2E-A-03`) in its docstring; the Stage 4.2 report refers to these IDs.

Safety: these tests use the same guarded fixtures as the rest of the suite (`tests/conftest.py`). The database
must be a local PostgreSQL whose name contains `test`; every test runs inside a transaction that is rolled back.
Feeds, Gmail, Supabase Auth and Gemini are test doubles: no network call, no API key, no quota, no email.

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/aicampus_test pytest tests/e2e -v`
