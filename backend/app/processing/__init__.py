"""Phase 3.2: news processing and deduplication.

Staged items from Stage 3.1 (`ingestion_items`) are normalised, deduplicated (exact, near-duplicate) and
grouped into story clusters with full source provenance. The output contract for Stage 3.3 is
`app.processing.contract.StoryCluster`; read it with `app.processing.service.list_story_clusters`.
Nothing here summarises, scores, publishes or deletes content.
"""
