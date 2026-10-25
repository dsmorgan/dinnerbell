# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check a rendered page for code the CSP forbids: inline scripts, ``style`` attributes, and
``on*`` event handlers. It parses the HTML as a browser would, so ``<SCRIPT>``, ``</script >``,
and ``ONCLICK=`` don't slip past, as they can past a regular expression."""

from __future__ import annotations

from html.parser import HTMLParser


class _Audit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.problems: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # The parser lowercases tag and attribute names.
        for name, _ in attrs:
            if name == "style" or name.startswith("on"):
                self.problems.append(f"<{tag} {name}=...>")
        if tag == "script":
            src = dict(attrs).get("src") or ""
            self.scripts.append(src)
            if not src.startswith("/static/"):
                self.problems.append(f"<script> without a /static/ src: {src!r}")
            self._in_script = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self._in_script = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.problems.append(f"inline script: {data.strip()[:40]!r}")


def inline_code(html: str) -> tuple[list[str], list[str]]:
    """The ``src`` of every script on the page, and every piece of inline code the CSP forbids."""
    audit = _Audit()
    audit.feed(html)
    audit.close()
    if audit._in_script:
        audit.problems.append("unclosed <script>")
    return audit.scripts, audit.problems
