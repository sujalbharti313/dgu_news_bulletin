"""Deterministic eligibility and protection rules for Stage 3.6 (SQL over database records only).

The same functions are used to stage a manifest and, inside the deletion transaction after the rows are locked, to
check every record again. A record is deleted only if it is a candidate of its job AND no protection applies at
deletion time. No AI output, client input or free-form SQL is ever involved.

Protections of story working data (a Stage 3.2 cluster's analysis, verification and embedding):
- permanent (state `protected`): selected in any edition (`in_edition_selection`), part of any edition revision
  (`in_edition_revision`), a candidate of a ranking used by any edition that is not published
  (`active_edition_candidate`), ranked for a week this job does not cover (`ranked_in_other_week`).
- temporary (state `skipped`, picked up again by a later orphans sweep): staged source items still exist, so the
  pipeline can still update, re-analyse or re-rank the story (`still_in_pipeline`); touched by the pipeline within
  the active window (`recently_active`); locked by another transaction right now (`locked`).
Protections of ranking runs: the run an edition was built from or a revision names (`referenced_by_edition`),
the latest completed run of its week (`latest_completed_run`), recently written (`recently_active`).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

PERMANENT = frozenset(
    {
        "in_edition_selection",
        "in_edition_revision",
        "active_edition_candidate",
        "ranked_in_other_week",
        "referenced_by_edition",
        "latest_completed_run",
        "database_protected",
    }
)
TEMPORARY = frozenset({"still_in_pipeline", "recently_active", "locked", "outside_scope"})
# The ranking-result arrays that list story clusters (Stage 3.4 contract, camelCase JSON).
RESULT_ARRAYS = ("candidates", "pending", "excluded", "failed")
MAX_ORPHAN_CANDIDATES = 5000


@dataclass(frozen=True)
class Scope:
    """What a job may delete. `weeks` = ranking weeks whose working data the job covers (for a published-week job,
    exactly its week); `before_week` additionally covers every ranking week before that Monday (orphans: weeks
    before the first edition week); `unranked` covers analyses that were never ranked (orphans only)."""

    weeks: tuple[date, ...]
    before_week: date | None = None
    unranked: bool = False


def _ids(values) -> list[uuid.UUID]:
    return [v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in values]


def _in_result(alias: str, cluster: str) -> str:
    return " OR ".join(
        f"{alias}.result -> '{arr}' @> jsonb_build_array(jsonb_build_object('storyClusterId', {cluster}::text))"
        for arr in RESULT_ARRAYS
    )


STORY_PROTECTION_SQL = f"""
WITH ids AS (SELECT unnest(CAST(:ids AS uuid[])) AS cluster_id)
SELECT ids.cluster_id,
       EXISTS (SELECT 1 FROM edition_items ei WHERE ei.cluster_id = ids.cluster_id) AS in_edition_selection,
       EXISTS (
           SELECT 1 FROM edition_revisions r
           WHERE r.content -> 'stories' @> jsonb_build_array(jsonb_build_object('cluster_id', ids.cluster_id::text))
       ) AS in_edition_revision,
       EXISTS (
           SELECT 1 FROM editions e
           JOIN ranking_runs rr ON rr.edition_start = e.week_start OR rr.id = e.ranking_run_id
           WHERE e.status <> 'published' AND ({_in_result("rr", "ids.cluster_id")})
       ) AS active_edition_candidate,
       EXISTS (
           SELECT 1 FROM story_verifications v
           WHERE v.cluster_id = ids.cluster_id
             AND NOT (v.edition_start = ANY(CAST(:weeks AS date[]))
                      OR (CAST(:before_week AS date) IS NOT NULL AND v.edition_start < CAST(:before_week AS date)))
       ) AS ranked_in_other_week,
       (:unranked = false AND NOT EXISTS (
           SELECT 1 FROM story_verifications v WHERE v.cluster_id = ids.cluster_id
       ) AND NOT EXISTS (
           SELECT 1 FROM ranking_runs rr
           WHERE rr.edition_start = ANY(CAST(:weeks AS date[])) AND ({_in_result("rr", "ids.cluster_id")})
       )) AS outside_scope,
       EXISTS (SELECT 1 FROM ingestion_items i WHERE i.cluster_id = ids.cluster_id) AS still_in_pipeline,
       (EXISTS (SELECT 1 FROM story_analyses a WHERE a.cluster_id = ids.cluster_id AND a.updated_at > :active_since)
        OR EXISTS (SELECT 1 FROM story_verifications v
                   WHERE v.cluster_id = ids.cluster_id AND v.updated_at > :active_since)
        OR EXISTS (SELECT 1 FROM story_embeddings m
                   WHERE m.cluster_id = ids.cluster_id AND m.updated_at > :active_since)) AS recently_active
FROM ids
"""  # noqa: S608 - built only from the RESULT_ARRAYS constants

STORY_REASON_ORDER = (
    "in_edition_selection",
    "in_edition_revision",
    "active_edition_candidate",
    "ranked_in_other_week",
    "outside_scope",
    "still_in_pipeline",
    "recently_active",
)


def story_protections(s: Session, cluster_ids, scope: Scope, active_since: datetime) -> dict[uuid.UUID, str | None]:
    """cluster id -> the first protection reason that applies, or None when the record may be deleted."""
    ids = _ids(cluster_ids)
    if not ids:
        return {}
    rows = s.execute(
        text(STORY_PROTECTION_SQL),
        {
            "ids": ids,
            "weeks": list(scope.weeks),
            "before_week": scope.before_week,
            "unranked": scope.unranked,
            "active_since": active_since,
        },
    ).mappings()
    out: dict[uuid.UUID, str | None] = {}
    for row in rows:
        out[row["cluster_id"]] = next((r for r in STORY_REASON_ORDER if row[r]), None)
    return out


RUN_PROTECTION_SQL = """
WITH ids AS (SELECT unnest(CAST(:ids AS uuid[])) AS run_id)
SELECT ids.run_id,
       (EXISTS (SELECT 1 FROM editions e WHERE e.ranking_run_id = ids.run_id)
        OR EXISTS (
            SELECT 1 FROM edition_revisions r
            WHERE r.content -> 'stories'
                  @> jsonb_build_array(jsonb_build_object('selection',
                         jsonb_build_object('ranking_run_id', ids.run_id::text)))
        )) AS referenced_by_edition,
       EXISTS (
           SELECT 1 FROM ranking_runs rr
           WHERE rr.id = ids.run_id AND rr.id = (
               SELECT l.id FROM ranking_runs l
               WHERE l.edition_start = rr.edition_start AND l.status = 'completed'
               ORDER BY l.processed_at DESC, l.id DESC LIMIT 1
           )
       ) AS latest_completed_run,
       EXISTS (
           SELECT 1 FROM ranking_runs rr
           WHERE rr.id = ids.run_id AND NOT rr.edition_start = ANY(CAST(:weeks AS date[]))
       ) AS ranked_in_other_week,
       EXISTS (SELECT 1 FROM ranking_runs rr WHERE rr.id = ids.run_id AND rr.updated_at > :active_since)
           AS recently_active
FROM ids
"""
RUN_REASON_ORDER = ("referenced_by_edition", "latest_completed_run", "ranked_in_other_week", "recently_active")


def run_protections(
    s: Session, run_ids, weeks: tuple[date, ...], active_since: datetime
) -> dict[uuid.UUID, str | None]:
    ids = _ids(run_ids)
    if not ids:
        return {}
    rows = s.execute(
        text(RUN_PROTECTION_SQL), {"ids": ids, "weeks": list(weeks), "active_since": active_since}
    ).mappings()
    return {row["run_id"]: next((r for r in RUN_REASON_ORDER if row[r]), None) for row in rows}


ORPHAN_VERIFICATION_PROTECTION_SQL = """
WITH ids AS (SELECT unnest(CAST(:ids AS uuid[])) AS cluster_id)
SELECT ids.cluster_id,
       EXISTS (SELECT 1 FROM story_analyses a WHERE a.cluster_id = ids.cluster_id) AS has_analysis,
       EXISTS (
           SELECT 1 FROM story_verifications v
           WHERE v.cluster_id = ids.cluster_id
             AND NOT (v.edition_start = ANY(CAST(:weeks AS date[]))
                      OR (CAST(:before_week AS date) IS NOT NULL AND v.edition_start < CAST(:before_week AS date)))
       ) AS ranked_in_other_week,
       EXISTS (SELECT 1 FROM ingestion_items i WHERE i.cluster_id = ids.cluster_id) AS still_in_pipeline,
       EXISTS (SELECT 1 FROM story_verifications v
               WHERE v.cluster_id = ids.cluster_id AND v.updated_at > :active_since) AS recently_active
FROM ids
"""


def orphan_verification_protections(
    s: Session, cluster_ids, scope: Scope, active_since: datetime
) -> dict[uuid.UUID, str | None]:
    ids = _ids(cluster_ids)
    if not ids:
        return {}
    rows = s.execute(
        text(ORPHAN_VERIFICATION_PROTECTION_SQL),
        {"ids": ids, "weeks": list(scope.weeks), "before_week": scope.before_week, "active_since": active_since},
    ).mappings()
    out = {}
    for row in rows:
        # An analysis appeared again: the cluster is handled as a whole story, not as an orphan row.
        reason = "has_analysis" if row["has_analysis"] else None
        reason = reason or next(
            (r for r in ("ranked_in_other_week", "still_in_pipeline", "recently_active") if row[r]), None
        )
        out[row["cluster_id"]] = reason
    return out


def is_temporary(reason: str | None) -> bool:
    return reason in TEMPORARY or reason == "has_analysis"


# ------------------------------------------------------------------------------------------------ candidates
def week_story_candidates(s: Session, week: date) -> list[uuid.UUID]:
    """Every cluster of a ranking week that still has working data: ranked for that week (story_verifications) or
    listed by any of that week's ranking runs."""
    arrays = " UNION ".join(
        f"SELECT (jsonb_array_elements(rr.result -> '{arr}') ->> 'storyClusterId')::uuid AS cluster_id "  # noqa: S608
        "FROM ranking_runs rr WHERE rr.edition_start = :week "
        f"AND jsonb_typeof(rr.result -> '{arr}') = 'array'"
        for arr in RESULT_ARRAYS
    )
    rows = s.execute(
        text(
            f"""
            WITH c AS (
                SELECT cluster_id FROM story_verifications WHERE edition_start = :week
                UNION {arrays}
            )
            SELECT c.cluster_id FROM c
            WHERE EXISTS (SELECT 1 FROM story_analyses a WHERE a.cluster_id = c.cluster_id)
               OR EXISTS (SELECT 1 FROM story_verifications v WHERE v.cluster_id = c.cluster_id)
               OR EXISTS (SELECT 1 FROM story_embeddings m WHERE m.cluster_id = c.cluster_id)
            ORDER BY c.cluster_id
            """  # noqa: S608 - built only from the RESULT_ARRAYS constants
        ),
        {"week": week},
    ).all()
    return [r[0] for r in rows]


def week_run_candidates(s: Session, week: date) -> list[uuid.UUID]:
    rows = s.execute(
        text("SELECT id FROM ranking_runs WHERE edition_start = :week ORDER BY processed_at, id"), {"week": week}
    ).all()
    return [r[0] for r in rows]


def orphan_story_candidates(s: Session, scope: Scope, now: datetime, older_than: datetime) -> list[uuid.UUID]:
    """Expired analyses (expires_at passed, untouched since `older_than`) that were never ranked, or ranked only
    for a week this sweep covers."""
    rows = s.execute(
        text(
            """
            SELECT a.cluster_id FROM story_analyses a
            LEFT JOIN story_verifications v ON v.cluster_id = a.cluster_id
            WHERE a.expires_at <= :now AND a.updated_at <= :older_than
              AND NOT EXISTS (SELECT 1 FROM edition_items ei WHERE ei.cluster_id = a.cluster_id)
              AND ((v.id IS NULL AND :unranked)
                   OR v.edition_start = ANY(CAST(:weeks AS date[]))
                   OR (CAST(:before_week AS date) IS NOT NULL AND v.edition_start < CAST(:before_week AS date)))
            ORDER BY a.cluster_id
            LIMIT :limit
            """
        ),
        {
            "now": now,
            "older_than": older_than,
            "unranked": scope.unranked,
            "weeks": list(scope.weeks),
            "before_week": scope.before_week,
            "limit": MAX_ORPHAN_CANDIDATES,
        },
    ).all()
    return [r[0] for r in rows]


def orphan_verification_candidates(s: Session, scope: Scope, now: datetime, older_than: datetime) -> list[uuid.UUID]:
    rows = s.execute(
        text(
            """
            SELECT v.cluster_id FROM story_verifications v
            WHERE NOT EXISTS (SELECT 1 FROM story_analyses a WHERE a.cluster_id = v.cluster_id)
              AND v.expires_at <= :now AND v.updated_at <= :older_than
              AND (v.edition_start = ANY(CAST(:weeks AS date[]))
                   OR (CAST(:before_week AS date) IS NOT NULL AND v.edition_start < CAST(:before_week AS date)))
            ORDER BY v.cluster_id
            LIMIT :limit
            """
        ),
        {
            "now": now,
            "older_than": older_than,
            "weeks": list(scope.weeks),
            "before_week": scope.before_week,
            "limit": MAX_ORPHAN_CANDIDATES,
        },
    ).all()
    return [r[0] for r in rows]
