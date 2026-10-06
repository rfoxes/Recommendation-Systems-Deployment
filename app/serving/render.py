"""Fills template/character_ad.html. Parsed once at startup into literal text and placeholder slots.

Each placeholder is escaped for where it sits: inside <script> it's a JavaScript string literal (quotes,
backslashes, newlines and `</script>` must not break out); elsewhere it's HTML text or an attribute.
That matters because CHAR_MESSAGE comes from an LLM. {{#MEDIA_IS_VIDEO}}/{{^MEDIA_IS_VIDEO}} sections
are resolved ahead of time, so rendering is just joining strings.
"""

import html
import json
import re
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "template" / "character_ad.html"

PLACEHOLDERS = frozenset(
    {"CHAR_NAME", "CAMPAIGN", "CHAR_MESSAGE", "CTA", "MEDIA_URL", "TRACKING_URL", "IMPRESSION_URL", "AD_ID",
     "API_URL", "API_KEY", "THEME", "DOWNLOADS"}
)
_SECTION = re.compile(r"\{\{([#^])(\w+)\}\}(.*?)\{\{/\2\}\}", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_SCRIPT = re.compile(r"<script\b.*?</script>", re.DOTALL | re.IGNORECASE)
_VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".ogv")


def escape_html(value: str) -> str:
    return html.escape(value, quote=True)


def escape_js_string(value: str) -> str:
    """For the inside of a double-quoted JS string in an HTML <script> block."""
    escaped = json.dumps(value)[1:-1]  # quotes, backslashes, control chars, U+2028/9
    return escaped.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def is_video(url: str) -> bool:
    return url.lower().split("?")[0].endswith(_VIDEO_EXTENSIONS)


class _Compiled:
    def __init__(self, text: str) -> None:
        scripts = [m.span() for m in _SCRIPT.finditer(text)]
        self.parts: list[str | tuple[str, bool]] = []  # literal text, or (placeholder, inside_script)
        cursor = 0
        for match in _PLACEHOLDER.finditer(text):
            name = match.group(1)
            if name not in PLACEHOLDERS:
                raise ValueError(f"template has an unknown placeholder {{{{ {name} }}}}")
            self.parts.append(text[cursor : match.start()])
            self.parts.append((name, any(start <= match.start() < end for start, end in scripts)))
            cursor = match.end()
        self.parts.append(text[cursor:])

    def render(self, values: dict[str, str]) -> str:
        out = []
        for part in self.parts:
            if isinstance(part, str):
                out.append(part)
            else:
                name, in_script = part
                value = values.get(name, "")
                out.append(escape_js_string(value) if in_script else escape_html(value))
        return "".join(out)


class AdTemplate:
    def __init__(self, path: Path = DEFAULT_TEMPLATE) -> None:
        text = path.read_text()
        self._variants = {flag: _Compiled(self._resolve_sections(text, flag)) for flag in (True, False)}

    @staticmethod
    def _resolve_sections(text: str, media_is_video: bool) -> str:
        def keep(match: re.Match[str]) -> str:
            kind, name, body = match.groups()
            if name != "MEDIA_IS_VIDEO":
                raise ValueError(f"template has an unknown section {name}")
            return body if (kind == "#") == media_is_video else ""

        return _SECTION.sub(keep, text)

    def render(self, values: dict[str, str]) -> str:
        return self._variants[is_video(values.get("MEDIA_URL", ""))].render(values)
