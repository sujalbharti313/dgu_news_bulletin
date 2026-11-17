-- Remove the table left behind by the rolled-back first Phase 3.3 attempt (old migration 0004).
--
-- Run this ONCE, by hand, only if the table story_intelligence still exists. It deletes that table (it never
-- held source records) and the old attempt's run records in ingestion_jobs. This cannot be undone.
--
-- Safe before or after the current Phase 3.3 migrations (0004 placeholder, 0005 story_analyses):
-- - It only deletes run records of the OLD attempt (source_key 'stage-3.3-intelligence'), never the current
--   engine's runs ('stage-3.3-analysis').
-- - It moves the Alembic version from the old 0004 back to 0003 only if the database is still at 0004;
--   `alembic upgrade head` then re-applies the no-op 0004 and 0005. At 0005 the version is left unchanged.
-- Everything runs in one transaction: if any step fails, nothing changes.

BEGIN;

DELETE FROM ingestion_jobs WHERE job_type = 'summarisation' AND source_key = 'stage-3.3-intelligence';

DROP TABLE IF EXISTS story_intelligence;

UPDATE alembic_version SET version_num = '0003' WHERE version_num = '0004';

COMMIT;
