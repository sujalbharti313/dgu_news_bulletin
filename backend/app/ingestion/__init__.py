"""Phase 3.1: automated news ingestion.

Pipeline, one module per concern:

    sources.py      validated source configuration (TOML file)          -> SourceConfig
    url_safety.py   SSRF guard: scheme/host/IP validation                -> used by http.py and normalize.py
    http.py         bounded, retrying, SSRF-safe HTTP client              -> FetchResponse
    providers/      fetch + parse per source type (rss.py, gmail.py)      -> CollectResult[RawItem]
    parsers/        newsletter body parsers (tldr.py)                     -> RawItem
    normalize.py    clean, validate and canonicalise                      -> NormalizedItem
    store.py        temporary staging + job bookkeeping (Postgres/memory)
    runner.py       orchestration: one isolated job per source            -> RunReport

Everything fetched (feeds, emails, pages) is untrusted data. It is parsed as data only, bounded in size,
and never interpreted as instructions. Nothing here publishes content.
"""
