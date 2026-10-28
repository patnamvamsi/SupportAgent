"""Confluence storage format to markdown (BL-051): just enough for heading chunks.

The knowledge layer chunks by ``#``-``###`` headings and reads plain text, so
the conversion keeps headings, paragraphs, list items, table rows and code
bodies, and drops markup. Macros (``ac:*``) contribute their text: a code
macro's ``ac:plain-text-body`` (CDATA) becomes a fenced block; anything else
(an image, a user mention, a status lozenge) contributes only its visible
text. Nothing in a page is executed or followed: links keep their text, not
their target.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "###", "h5": "###", "h6": "###"}
_BLOCKS = frozenset({"p", "div", "blockquote", "tr", "ac:task-body"})
_SKIP = frozenset({"script", "style", "ac:parameter"})
_SPACE = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


class _Markdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._line: list[str] = []
        self._skip = 0
        self._pre = 0
        self._cell = False

    def _flush(self, *, gap: bool = False) -> None:
        text = _SPACE.sub(" ", "".join(self._line)).strip()
        self._line = []
        if text:
            self._out.append(text)
        if gap and self._out and self._out[-1] != "":
            self._out.append("")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag in _HEADINGS:
            self._flush(gap=True)
            self._line.append(_HEADINGS[tag] + " ")
        elif tag == "li":
            self._flush()
            self._line.append("- ")
        elif tag in _BLOCKS:
            self._flush(gap=tag != "tr")
        elif tag in {"td", "th"}:
            if self._cell:
                self._line.append(" | ")
            self._cell = True
        elif tag == "br":
            self._flush()
        elif tag in {"pre", "ac:plain-text-body"}:
            self._flush(gap=True)
            self._pre += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _HEADINGS or tag in _BLOCKS:
            self._flush(gap=tag != "tr")
            self._cell = False
        elif tag == "li":
            self._flush()
        elif tag in {"pre", "ac:plain-text-body"}:
            self._pre = max(0, self._pre - 1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._pre:
            self._out.extend(["```", data.strip("\n"), "```", ""])
            return
        self._line.append(data)

    def unknown_decl(self, data: str) -> None:
        # ``<![CDATA[...]]>`` inside a code macro's plain-text body.
        if data.startswith("CDATA["):
            self.handle_data(data[len("CDATA[") :])

    def markdown(self) -> str:
        self._flush()
        return _BLANKS.sub("\n\n", "\n".join(self._out)).strip() + "\n"


def storage_to_markdown(storage: str) -> str:
    """``storage`` (Confluence's XHTML body) as markdown headings and plain text."""
    parser = _Markdown()
    parser.feed(storage)
    parser.close()
    return parser.markdown()


__all__ = ["storage_to_markdown"]
