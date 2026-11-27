import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class PreparedText:
    text: str
    headings: tuple[tuple[int, str], ...]

    def section_at(self, offset: int) -> str:
        for position, label in reversed(self.headings):
            if position <= offset:
                return label
        return ""


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden:
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.parts.append("\n# ")
            elif tag in {"p", "div", "br", "li", "section", "tr"}:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "p",
            "div",
            "li",
            "section",
            "tr",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def prepare_text(content: bytes) -> PreparedText:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("invalid_encoding") from None
    parser = _PlainHTML()
    parser.feed(decoded)
    plain = "".join(parser.parts)
    lines: list[str] = []
    headings: list[tuple[int, str]] = []
    offset = 0
    for raw in plain.splitlines():
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", raw)
        line = re.sub(r"[*`]+", "", line)
        line = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", line)
        line = " ".join(line.split())
        heading = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", line)
        if heading:
            line = heading.group(1)
            headings.append((offset, line))
        if line:
            lines.append(line)
            offset += len(line) + 1
    text = "\n".join(lines)
    if not text.strip():
        raise ValueError("empty_content")
    return PreparedText(text=text, headings=tuple(headings))
