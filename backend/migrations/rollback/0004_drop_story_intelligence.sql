-- Roll the database back from Phase 3.3 (migration 0004) to Phase 3.2 (migration 0003).
--
-- Run this ONCE, by hand, against a database that is at Alembic revision 0004.
-- It deletes the story_intelligence table and every row in it, plus any Phase 3.3
-- run records in ingestion_jobs. This cannot be undone.
--
-- It mirrors the downgrade() of the removed migration 0004 and then moves the
-- Alembic version back to 0003, so `alembic upgrade head` keeps working with the
-- Phase 3.2 code. Everything runs in one transaction: if any step fails, nothing changes.

BEGIN;

DELETE FROM ingestion_jobs WHERE job_type = 'summarisation';

DROP TABLE IF EXISTS story_intelligence;

UPDATE alembic_version SET version_num = '0003' WHERE version_num = '0004';

COMMIT;
