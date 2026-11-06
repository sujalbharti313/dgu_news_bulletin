"""URL canonicalisation used to detect duplicate articles before they reach the database.

Two URLs that differ only in tracking parameters, fragment, host case, default port or a trailing slash
are treated as the same article. Query parameters that may identify content are kept (sorted).

The canonical URL is an identity key for deduplication, not a link to show readers: it always uses https
and drops `www.`, which some servers treat differently. The URL as published (`original_url`) is kept
separately everywhere and is the one to link to.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that only carry campaign/click attribution. They never select which article is served.
TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "oly_", "vero_", "_hs")
TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "yclid",
    "twclid",
    "ttclid",
    "li_fat_id",
    "igshid",
    "mkt_tok",
    "_ga",
    "_gl",
    "ref",
    "ref_src",
    "ref_url",
    "cmpid",
    "s_cid",
    "wickedid",
    "_hsenc",
    "_hsmi",
    "mbid",
}
DEFAULT_PORTS = {"http": 80, "https": 443}


def is_tracking_parameter(key: str) -> bool:
    key = key.lower()
    return key.startswith(TRACKING_PREFIXES) or key in TRACKING_KEYS


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        raise ValueError("Only absolute http(s) URLs can be canonicalised.")
    host = parts.hostname.lower().rstrip(".").removeprefix("www.")
    netloc = host if parts.port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_parameter(k))
    # Fragments are dropped (in-page anchors, share widgets), except hash-bang routes ("#!/post/42"),
    # which single-page sites use to select the article itself.
    fragment = parts.fragment if parts.fragment.startswith("!") else ""
    # https and http versions of the same article are the same article.
    return urlunsplit(("https", netloc, path, urlencode(query), fragment))
