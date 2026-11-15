"""HTML -> normalized text, stdlib only (no bs4).

The detection haystack is raw HTML + this normalized text: JSON-LD signals
(e.g. schema.org availability) live in the raw HTML, human-visible strings
("Out of stock") in the text.
"""
from __future__ import annotations

from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "noscript", "template"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:
        # a malformed page must not kill a poll; fall back to a crude strip
        import re
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())
    return " ".join(p.parts)


def normalize(text: str) -> str:
    return " ".join((text or "").split())
