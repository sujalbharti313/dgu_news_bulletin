"""Content normalisation for Stage 3.2: readable text, titles, publishers, dates and injection flags.

Everything here is deterministic and works on untrusted input. It builds on Stage 3.1's
`app.ingestion.text` (markup stripping, control/bidi character removal) and never invents a value:
a missing or unusable field comes back as None, with the reason, rather than a guess.
"""

import html
import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

from app.ingestion.text import clean_text

# --- readable text from HTML ------------------------------------------------------------------------

# Elements whose content is never article text.
_DROP_TAGS = {
    "script", "style", "head", "title", "noscript", "template", "svg", "iframe", "object", "embed",
    "nav", "header", "footer", "aside", "form", "button", "select", "textarea", "menu", "dialog",
}  # fmt: skip
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "main", "blockquote", "pre",
    "figure", "figcaption", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}  # fmt: skip
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "track"}
# class/id/role hints for navigation and page chrome (matched as whole words).
_BOILERPLATE_HINT = re.compile(
    r"(?:^|[\s_-])(nav|navbar|menu|breadcrumbs?|footer|header|sidebar|share|sharing|social|newsletter|"
    r"subscribe|signup|cookie|consent|banner|advert|ads?|promo|related|comments?|pagination|skip)(?:$|[\s_-])",
    re.I,
)
_BOILERPLATE_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "dialog"}
# Short lines that are page furniture, not content.
_BOILERPLATE_LINE = re.compile(
    r"^(share( this( article| post)?)?|tweet|subscribe( now)?|sign up|log ?in|sign in|read more|continue reading|"
    r"advertisement|sponsored|skip to (main )?content|back to top|related (articles|posts|stories)|"
    r"(accept|manage) cookies|cookie (settings|policy)|all rights reserved.*|©.*|copyright.*|"
    r"follow us( on .*)?|click here.*|loading\W*)$",
    re.I,
)


class _ReadableExtractor(HTMLParser):
    """Collects text block by block, skipping chrome. Tracks <article>/<main> so their text can win."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[bool, str]] = []  # (inside article/main, text)
        self._current: list[str] = []
        self._stack: list[tuple[str, bool]] = []  # (tag, is_skipping)
        self._skip_depth = 0
        self._main_depth = 0
        self._pre_depth = 0

    def _flush(self) -> None:
        text = "".join(self._current)
        self._current = []
        if self._pre_depth:
            text = text.strip("\n")
        else:
            text = re.sub(r"\s+", " ", text).strip()
        if text:
            self.blocks.append((self._main_depth > 0, text))

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            if tag in _BLOCK_TAGS and not self._skip_depth:
                self._flush()
            return
        attributes = {k: (v or "") for k, v in attrs}
        hint = f"{attributes.get('class', '')} {attributes.get('id', '')}"
        skipping = (
            tag in _DROP_TAGS
            or attributes.get("role", "").lower() in _BOILERPLATE_ROLES
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
            or bool(_BOILERPLATE_HINT.search(hint))
        )
        if tag in _BLOCK_TAGS and not self._skip_depth:
            self._flush()
        self._stack.append((tag, skipping))
        if skipping:
            self._skip_depth += 1
        if tag in ("article", "main"):
            self._main_depth += 1
        if tag == "pre":
            self._pre_depth += 1

    def handle_endtag(self, tag):
        # Pop to the matching open tag; tolerate unclosed and stray tags.
        if not any(t == tag for t, _ in self._stack):
            return
        while self._stack:
            open_tag, skipping = self._stack.pop()
            if open_tag in _BLOCK_TAGS and not self._skip_depth:
                self._flush()
            if skipping:
                self._skip_depth -= 1
            if open_tag in ("article", "main"):
                self._main_depth -= 1
            if open_tag == "pre":
                self._pre_depth -= 1
            if open_tag == tag:
                break

    def handle_data(self, data):
        if not self._skip_depth:
            self._current.append(data)

    def close(self):
        super().close()
        self._flush()


def extract_readable_text(markup: str, *, max_length: int = 20_000) -> str:
    """Readable plain text from an HTML page or fragment: paragraphs kept, navigation and chrome dropped.

    If the page has an <article> or <main> element with substantial text, only that text is used.
    Technical content (code, version strings, numbers, quoted limitations) is kept verbatim.
    """
    if not markup:
        return ""
    if "<" not in markup:
        return normalize_text(markup, max_length=max_length)
    parser = _ReadableExtractor()
    try:
        parser.feed(markup)
        parser.close()
        blocks = parser.blocks
    except Exception:  # noqa: BLE001 - malformed markup: fall back to tag stripping
        blocks = [(False, html.unescape(re.sub(r"<[^>]*>", " ", markup)))]
    main_text = [text for in_main, text in blocks if in_main]
    if sum(len(t) for t in main_text) >= 200:
        blocks = [(True, t) for t in main_text]
    paragraphs = [text for _, text in blocks if not _BOILERPLATE_LINE.match(text.strip())]
    return normalize_text("\n\n".join(paragraphs), max_length=max_length)


# --- plain text ---------------------------------------------------------------------------------------

_SPACES = re.compile(r"[^\S\n]+")
# Hyphen look-alikes become "-" so "GPT‑5.1" (non-breaking hyphen) and "GPT-5.1" are the same text.
HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2212": "-", "\u00ad": ""})
_MANY_NEWLINES = re.compile(r"\n{3,}")


def _strip_invisible(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\t")


def repair_mojibake(text: str) -> str:
    """Undo the common UTF-8-read-as-Latin-1 corruption ("Ã©", "â€™"), only when the round trip is clean."""
    if not re.search(r"[ÃÂâ][\x80-\xbf€™œžŸ“”‘’–—•…]", text):
        return text
    for encoding in ("cp1252", "latin-1"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != text:
            return repaired
    return text


def normalize_text(value: str | None, *, max_length: int = 20_000) -> str:
    """Normalise plain text: entities decoded, Unicode NFKC, invisible characters removed, spaces collapsed,
    paragraph breaks kept (at most one blank line). Truncated on a word boundary with an ellipsis."""
    if not value:
        return ""
    text = repair_mojibake(html.unescape(value))
    text = unicodedata.normalize("NFKC", text).translate(HYPHENS).replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_invisible(text.replace("\t", " "))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = _MANY_NEWLINES.sub("\n\n", "\n".join(lines)).strip()
    if len(text) > max_length:
        cut = text[: max_length - 1]
        space = cut.rfind(" ")
        if space > max_length // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,.;:-") + "…"
    return text


def normalize_body(value: str | None, *, max_length: int) -> str | None:
    """An excerpt or article body: readable text when it is HTML, otherwise normalised plain text."""
    if not value:
        return None
    text = (
        extract_readable_text(value, max_length=max_length)
        if "<" in value
        else normalize_text(value, max_length=max_length)
    )
    return text or None


# --- titles -------------------------------------------------------------------------------------------

MAX_TITLE = 500
_GENERIC_TITLES = {
    "untitled", "no title", "(no title)", "title", "home", "homepage", "index", "news", "blog", "read more",
    "click here", "404", "page not found", "not found", "error", "loading", "null", "none", "undefined", "n/a",
}  # fmt: skip
_URLISH = re.compile(r"^(https?://|www\.)\S+$", re.I)


class InvalidField(ValueError):
    """A field is present but unusable. The message is a short, safe reason code."""


def normalize_title(value: str | None) -> str:
    """Clean a headline. Raises InvalidField (never substitutes another value) when it is unusable."""
    title = clean_text(value, MAX_TITLE) if value else None
    if not title:
        raise InvalidField("missing_title")
    title = repair_mojibake(title).translate(HYPHENS)
    if _URLISH.match(title):
        raise InvalidField("title_is_url")
    if title.strip(" .!?-–—:|").casefold() in _GENERIC_TITLES:
        raise InvalidField("generic_title")
    if sum(ch.isalnum() for ch in title) < 3:
        raise InvalidField("title_too_short")
    return title


# --- publishers and sites -----------------------------------------------------------------------------

_FEED_SUFFIX = re.compile(r"\s*(?:[-–—|:]\s*)?(?:rss|atom)(?:\s+feed)?\s*$|\s*[-–—|:]\s*(?:feed|latest news)\s*$", re.I)


def normalize_publisher(value: str | None) -> str | None:
    """Tidy a publisher name ("OpenAI News - RSS" → "OpenAI News"). Never derives one from the URL."""
    name = clean_text(value, 200) if value else None
    if not name:
        return None
    name = _FEED_SUFFIX.sub("", name).strip(" -–—|:")
    return name or None


def site_of(url: str | None) -> str | None:
    """The host an article was published on (lower-case, no www.). A fact from the URL, not a publisher name."""
    if not url:
        return None
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    return host.removeprefix("www.") or None


# --- dates --------------------------------------------------------------------------------------------

EARLIEST_PLAUSIBLE = datetime(1990, 1, 1, tzinfo=UTC)
FUTURE_TOLERANCE = timedelta(days=1)


def normalize_datetime(value: object, *, now: datetime) -> datetime | None:
    """A timezone-aware UTC datetime, or None when missing, unparseable or implausible.

    Accepts datetimes (naive ones are taken as UTC), dates and ISO-8601 strings. Values before 1990 or more
    than a day in the future are rejected rather than corrected."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                value = date.fromisoformat(value.strip())
            except ValueError:
                return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day, tzinfo=UTC)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    if value < EARLIEST_PLAUSIBLE or value > now + FUTURE_TOLERANCE:
        return None
    return value


# --- untrusted content: prompt-injection markers ------------------------------------------------------

_SPECIAL_TOKENS = re.compile(r"<\|[^|<>\n]{0,40}\|>|\[/?INST\]|<</?SYS>>", re.I)
_INSTRUCTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|preceding|your)\s+"
        r"(instructions?|prompts?|directions?|rules|messages?)",
        r"\b(system|developer)\s+(prompt|message|instructions?)\s*:",
        r"\byou\s+are\s+now\s+(a|an|the|in)\b",
        r"\bnew\s+instructions?\s*:",
        r"</?\s*(system|assistant|user|instructions?)\s*>",
        r"^\s*#{1,6}\s*(system|instructions?)\b",
        r"\b(respond|reply|answer)\s+only\s+with\b",
    )
]


def neutralize_special_tokens(text: str) -> str:
    """Remove chat-template control tokens (`<|im_start|>`, `[INST]`, `<<SYS>>`) that have no meaning in news."""
    return _SPECIAL_TOKENS.sub(" ", text)


def instruction_flags(*texts: str | None) -> list[str]:
    """Flags for text that looks like instructions aimed at an AI model. The text is kept; it is only marked."""
    flags: set[str] = set()
    for text in texts:
        if not text:
            continue
        if _SPECIAL_TOKENS.search(text):
            flags.add("chat_template_tokens")
        if any(p.search(text) for p in _INSTRUCTION_PATTERNS):
            flags.add("possible_prompt_injection")
    return sorted(flags)
