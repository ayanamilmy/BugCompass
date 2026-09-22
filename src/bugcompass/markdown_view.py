from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RenderSpan:
    text: str
    tags: tuple[str, ...] = ()


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
QUOTE_RE = re.compile(r"^>\s?(.*)$")
SEPARATOR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
INLINE_RE = re.compile(
    r"`([^`]+)`"
    r"|\*\*(.+?)\*\*"
    r"|__(.+?)__"
    r"|\[([^\]]+)\]\(([^)]+)\)"
    r"|(?<!\*)\*([^*\n]+)\*(?!\*)"
    r"|(?<!_)_([^_\n]+)_(?!_)"
)


def render_markdown(markdown: str) -> list[RenderSpan]:
    """把常用 Markdown 转成适合 Tk Text 插入的文本与样式片段。"""
    spans: list[RenderSpan] = []
    in_code_block = False
    for line in markdown.splitlines(keepends=True):
        content = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""

        if content.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            spans.append(RenderSpan(content + newline, ("code_block",)))
            continue

        heading = HEADING_RE.match(content)
        if heading:
            level = min(len(heading.group(1)), 3)
            spans.extend(_inline_spans(heading.group(2), (f"heading{level}",)))
            if newline:
                spans.append(RenderSpan(newline, (f"heading{level}",)))
            continue

        bullet = BULLET_RE.match(content)
        if bullet:
            indent = "  " * (len(bullet.group(1).expandtabs(4)) // 2)
            spans.append(RenderSpan(f"{indent}• ", ("list",)))
            spans.extend(_inline_spans(bullet.group(2), ("list",)))
            if newline:
                spans.append(RenderSpan(newline, ("list",)))
            continue

        ordered = ORDERED_RE.match(content)
        if ordered:
            indent = "  " * (len(ordered.group(1).expandtabs(4)) // 2)
            spans.append(RenderSpan(f"{indent}{ordered.group(2)}. ", ("list",)))
            spans.extend(_inline_spans(ordered.group(3), ("list",)))
            if newline:
                spans.append(RenderSpan(newline, ("list",)))
            continue

        quote = QUOTE_RE.match(content)
        if quote:
            spans.append(RenderSpan("▎ ", ("quote",)))
            spans.extend(_inline_spans(quote.group(1), ("quote",)))
            if newline:
                spans.append(RenderSpan(newline, ("quote",)))
            continue

        if SEPARATOR_RE.match(content):
            spans.append(RenderSpan("────────────────────────\n" if newline else "────────────────────────", ("separator",)))
            continue

        spans.extend(_inline_spans(content, ("body",)))
        if newline:
            spans.append(RenderSpan(newline, ("body",)))
    return spans


def plain_text(markdown: str) -> str:
    return "".join(span.text for span in render_markdown(markdown))


def _inline_spans(text: str, base_tags: tuple[str, ...]) -> list[RenderSpan]:
    spans: list[RenderSpan] = []
    cursor = 0
    for match in INLINE_RE.finditer(text):
        if match.start() > cursor:
            spans.append(RenderSpan(text[cursor : match.start()], base_tags))
        if match.group(1) is not None:
            spans.append(RenderSpan(match.group(1), base_tags + ("inline_code",)))
        elif match.group(2) is not None:
            spans.append(RenderSpan(match.group(2), base_tags + ("bold",)))
        elif match.group(3) is not None:
            spans.append(RenderSpan(match.group(3), base_tags + ("bold",)))
        elif match.group(4) is not None:
            label, url = match.group(4), match.group(5)
            spans.append(RenderSpan(f"{label}（{url}）", base_tags + ("link",)))
        elif match.group(6) is not None:
            spans.append(RenderSpan(match.group(6), base_tags + ("italic",)))
        else:
            spans.append(RenderSpan(match.group(7), base_tags + ("italic",)))
        cursor = match.end()
    if cursor < len(text):
        spans.append(RenderSpan(text[cursor:], base_tags))
    return spans
