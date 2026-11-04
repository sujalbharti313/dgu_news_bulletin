"""TLDR AI newsletter parser.

TLDR lists each article as a link whose text ends with a label such as "(4 minute read)" or
"(GitHub Repo)", followed by a short blurb. Links go through TLDR's click tracker
(`tracking.tldrnewsletter.com/CL0/<percent-encoded destination>/...`); the destination is decoded from the
link itself, never by following the redirect, so no request is made and no subscriber identifier is kept.
Links that cannot be decoded are dropped and counted rather than guessed. Sponsored entries are skipped.

The email is untrusted input: only titles, links and short blurbs are extracted, the body is never stored,
and nothing in it is treated as an instruction.

The format is TLDR's to change. Unknown layouts produce zero items plus a warning, never invented items.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

LABEL = r"\d+\s+minute\s+read|github\s+repo|website|paper|podcast|video|tool|research|course"
ARTICLE_LABEL = re.compile(rf"\(\s*(?P<label>{LABEL})\s*\)\s*$", re.IGNORECASE)
SPONSOR = re.compile(r"\(\s*sponsor(ed)?\s*\)", re.IGNORECASE)
READ_TIME = re.compile(r"(\d+)\s+minute\s+read", re.IGNORECASE)
TLDR_DOMAINS = ("tldrnewsletter.com", "tldr.tech")
MAX_BLURB = 1200
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "table", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "section"}
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template"}


@dataclass
class NewsletterArticle:
    title: str
    url: str
    description: str | None
    label: str
    read_minutes: int | None = None


@dataclass
class ParseOutcome:
    articles: list[NewsletterArticle] = field(default_factory=list)
    sponsored: int = 0
    unresolved_links: int = 0
    warnings: list[str] = field(default_factory=list)


def resolve_link(href: str) -> str | None:
    """Return the destination URL of a newsletter link, or None if it is internal/undecodable."""
    href = (href or "").strip()
    if not href.lower().startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host == "tldrnewsletter.com" or host.endswith(".tldrnewsletter.com"):
        marker = "/CL0/"
        if marker not in parts.path:
            return None
        encoded = parts.path.split(marker, 1)[1].split("/", 1)[0]
        target = unquote(encoded)
        if target.lower().startswith(("http://", "https://")) and not _is_tldr_host(target):
            return target
        return None
    if _is_tldr_host(href):
        return None  # TLDR's own pages: archive, sign-up, advertise, referral links
    return href


def _is_tldr_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in TLDR_DOMAINS)


class _Segments(HTMLParser):
    """Flattens HTML into ("text", str) and ("link", href, text) segments in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[tuple] = []
        self._text: list[str] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._skip = 0

    def _flush_text(self) -> None:
        if self._text:
            self.segments.append(("text", "".join(self._text)))
            self._text = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "a":
            self._flush_text()
            self._link_href = dict(attrs).get("href") or ""
            self._link_text = []
        elif tag in BLOCK_TAGS:
            (self._link_text if self._link_href is not None else self._text).append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "a" and self._link_href is not None:
            self.segments.append(("link", self._link_href, "".join(self._link_text)))
            self._link_href = None
        elif tag in BLOCK_TAGS:
            self._text.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        (self._link_text if self._link_href is not None else self._text).append(data)

    def close(self):
        super().close()
        if self._link_href is not None:
            self.segments.append(("link", self._link_href, "".join(self._link_text)))
            self._link_href = None
        self._flush_text()


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _split_title(text: str) -> tuple[str, str] | None:
    text = _squash(text)
    match = ARTICLE_LABEL.search(text)
    if not match:
        return None
    title = text[: match.start()].strip(" -–—:")
    return (title, match.group("label")) if title else None


def _read_minutes(label: str) -> int | None:
    match = READ_TIME.search(label)
    return int(match.group(1)) if match else None


def parse_tldr_html(html_body: str) -> ParseOutcome:
    collector = _Segments()
    collector.feed(html_body)
    collector.close()

    outcome = ParseOutcome()
    current: dict | None = None

    def finish():
        if current is not None:
            blurb = _squash(" ".join(current["blurb"]))[:MAX_BLURB] or None
            outcome.articles.append(
                NewsletterArticle(
                    current["title"], current["url"], blurb, current["label"], _read_minutes(current["label"])
                )
            )

    for segment in collector.segments:
        if segment[0] == "link":
            _, href, text = segment
            if SPONSOR.search(text):
                finish()
                current = None
                outcome.sponsored += 1
                continue
            split = _split_title(text)
            if split is not None:
                finish()
                current = None
                url = resolve_link(href)
                if url is None:
                    outcome.unresolved_links += 1
                    continue
                current = {"title": split[0], "label": split[1], "url": url, "blurb": [], "closed": False}
                continue
            if current is not None and not current["closed"]:
                current["blurb"].append(text)  # inline link inside a blurb: keep its words only
        elif current is not None and not current["closed"]:
            # The blurb is the first paragraph after the title; a blank line (e.g. <br><br>) ends it, so
            # section headers and the newsletter footer never leak into the previous article.
            for index, paragraph in enumerate(_PARAGRAPH_BREAK.split(segment[1])):
                if index > 0 and _squash(" ".join(current["blurb"])):
                    current["closed"] = True
                    break
                current["blurb"].append(paragraph)
    finish()
    return outcome


_TEXT_TITLE = re.compile(rf"^(?P<title>.+?)\s*\(\s*(?P<label>{LABEL})\s*\)\s*(?:\[(?P<ref>\d+)\])?\s*$", re.IGNORECASE)
_TEXT_INLINE_URL = re.compile(r"(https?://\S+)")
_TEXT_REF = re.compile(r"^\s*\[(\d+)\]\s+(https?://\S+)\s*$")


def parse_tldr_text(text_body: str) -> ParseOutcome:
    """Fallback for plain-text emails: "Title (N minute read) [ref]" lines, refs listed as "[ref] url"."""
    lines = text_body.splitlines()
    refs = {m.group(1): m.group(2) for m in (_TEXT_REF.match(line) for line in lines) if m}
    outcome = ParseOutcome()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        url = None
        inline = _TEXT_INLINE_URL.search(line)
        candidate = _TEXT_INLINE_URL.sub("", line).strip() if inline else line
        match = _TEXT_TITLE.match(candidate)
        i += 1
        if _TEXT_REF.match(line):
            continue
        if SPONSOR.search(line) and re.search(r"\[\d+\]\s*$|https?://", line):
            outcome.sponsored += 1
            continue
        if not match:
            continue
        if inline:
            url = inline.group(1)
        elif match.group("ref"):
            url = refs.get(match.group("ref"))
        resolved = resolve_link(url) if url else None
        if resolved is None:
            outcome.unresolved_links += 1
            continue
        blurb: list[str] = []
        while i < len(lines) and not lines[i].strip():
            i += 1
        while i < len(lines) and lines[i].strip() and not _TEXT_TITLE.match(lines[i].strip()):
            if not _TEXT_REF.match(lines[i]):
                blurb.append(lines[i].strip())
            i += 1
        title = match.group("title").strip(" -–—:")
        outcome.articles.append(
            NewsletterArticle(
                title,
                resolved,
                _squash(" ".join(blurb))[:MAX_BLURB] or None,
                match.group("label"),
                _read_minutes(match.group("label")),
            )
        )
    return outcome


def parse_tldr(html_body: str | None, text_body: str | None) -> ParseOutcome:
    if html_body:
        outcome = parse_tldr_html(html_body)
        if outcome.articles:
            return outcome
    if text_body:
        outcome = parse_tldr_text(text_body)
        if outcome.articles:
            return outcome
    outcome = ParseOutcome()
    outcome.warnings.append("no articles recognised; the newsletter layout may have changed")
    return outcome
