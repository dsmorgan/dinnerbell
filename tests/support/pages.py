# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check a rendered page for code the CSP forbids: inline scripts, ``<style>`` elements, ``style``
attributes, ``on*``, ``hx-on``, and ``data-hx-on`` event handlers, ``javascript:`` URLs, ``srcdoc``
documents with any script in them, and script sources that climb out of ``/static/``. It parses the HTML as
a browser would, so ``<SCRIPT>``, ``</script >``, and ``ONCLICK=`` don't slip past, as they can
past a regular expression."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import unquote


def _is_javascript_url(value: str | None) -> bool:
    """Whether a browser would run ``value`` as a URL: it ignores case, leading control characters
    and spaces, and tabs and newlines anywhere inside the scheme."""
    if value is None:
        return False
    squeezed = "".join(ch for ch in value.lstrip("".join(map(chr, range(33)))) if ch not in "\t\n\r")
    return squeezed.lower().startswith("javascript:")


class _Audit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.problems: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # The parser lowercases tag and attribute names.
        for name, value in attrs:
            # HTMX runs hx-on:* and data-hx-on:* as script, but data-onboarding is only data.
            if name == "style" or name.startswith("on") or name.removeprefix("data-").startswith("hx-on"):
                self.problems.append(f"<{tag} {name}=...>")
            elif name == "srcdoc" and value is not None and any(inline_code(value)):
                self.problems.append(f"<{tag} srcdoc=...> with a script or inline code")
            elif _is_javascript_url(value):
                self.problems.append(f"<{tag} {name}=javascript:...>")
        if tag == "style":
            self.problems.append("<style> element")
        if tag == "script":
            src = dict(attrs).get("src") or ""
            self.scripts.append(src)
            if not src.startswith("/static/") or ".." in unquote(src):
                self.problems.append(f"<script> without a /static/ src: {src!r}")
            self._in_script = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag == "script":
            # HTML has no self-closing script: a browser reads ``<script src=.../>`` as an open tag
            # and runs whatever follows, up to ``</script>``. So flag it, and keep reading as script.
            self.problems.append("self-closing <script>")

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
