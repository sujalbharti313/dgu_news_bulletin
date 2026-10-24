"""URL canonicalisation used to detect duplicate articles before they reach the database.

Two URLs that differ only in tracking parameters, fragment, host case, default port or a trailing slash
are treated as the same article. Query parameters that may identify content are kept (sorted).
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_", "mc_", "pk_")
TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "ref", "ref_src", "cmpid", "_hsenc", "_hsmi"}
DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        raise ValueError("Only absolute http(s) URLs can be canonicalised.")
    host = parts.hostname.lower().removeprefix("www.")
    netloc = host if parts.port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_KEYS
    )
    # https and http versions of the same article are the same article.
    return urlunsplit(("https", netloc, path, urlencode(query), ""))
