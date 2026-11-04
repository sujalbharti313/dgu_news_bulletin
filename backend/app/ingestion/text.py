"""Turning untrusted HTML/text fragments into short, plain, bounded strings."""

import html
import re
import unicodedata
from html.parser import HTMLParser

_WS = re.compile(r"\s+")
_SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template", "svg"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: fall back to tag stripping
        return html.unescape(re.sub(r"<[^>]*>", " ", value))
    return "".join(parser.parts)


def clean_text(value: str | None, max_length: int, *, is_html: bool = True) -> str | None:
    """Strip markup, control and invisible characters, collapse whitespace, and truncate on a word."""
    if value is None:
        return None
    text = html_to_text(value) if is_html and "<" in value else html.unescape(value)
    text = unicodedata.normalize("NFKC", text)
    # Drop control and format characters (zero-width, bidi overrides) that can hide or reorder text.
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\t\n ")
    text = _WS.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > max_length:
        cut = text[: max_length - 1]
        space = cut.rfind(" ")
        if space > max_length // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,.;:-") + "…"
    return text
