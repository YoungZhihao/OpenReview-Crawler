"""A small Markdown -> HTML converter, sized for OpenReview review text.

Why not use a library: the project is standard-library-only on purpose (it has
to run on a machine where ``pip install`` is unavailable), and pulling in a full
CommonMark implementation for review prose would be a large dependency for a
small job.

What it handles -- the subset reviewers and authors actually type:

* headings, paragraphs, hard line breaks (reviewers write line by line, so a
  single newline becomes ``<br>`` rather than a space)
* bullet and numbered lists, including one level of nesting
* fenced code blocks, inline code spans
* blockquotes, horizontal rules, simple pipe tables
* ``**bold**``, ``*italic*``, ``_italic_``, ``~~strike~~``, ``[links](url)``,
  bare URL autolinking

Safety and fidelity rules, both deliberate:

* **Input is HTML-escaped first.** Markdown is then applied to the escaped text,
  so a ``<script>`` in a review can never become markup. There is no raw-HTML
  passthrough, unlike CommonMark.
* **LaTeX is protected before anything else.** ``$x_i$`` and ``\\(x\\)`` are
  lifted out into placeholders, so the underscores and asterisks inside maths
  are never mistaken for emphasis.
* Code spans are protected too, for the same reason.

This is a best-effort renderer, not a spec-complete one. The HTML report keeps
the original text alongside the rendered version and offers a one-click toggle,
so a mangled edge case is always recoverable.
"""

from __future__ import annotations

import html
import re

from .mathrender import render_math

# --------------------------------------------------------------------------- #
# inline patterns
# --------------------------------------------------------------------------- #
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_HRULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
# Note the two spellings of ">": the input is HTML-escaped before block
# parsing, so a blockquote arrives as "&gt; quoted text". Matching only the raw
# character silently turned every blockquote into a paragraph.
_QUOTE = re.compile(r"^\s*(?:>|&gt;)\s?")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$")

# Math first (longest delimiters first), then inline.
_MATH_BLOCK = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]", re.S)
# An escaped "\$" is a literal dollar sign, never a delimiter -- standard LaTeX
# and Markdown semantics. Without the lookbehind, a reviewer quoting a word
# problem ("$23. She bought five bagels for \$") had the whole sentence
# detected as maths and rendered as italic variables.
_MATH_INLINE = re.compile(r"(?<!\\)\$(?!\$).+?(?<!\\)\$|\\\(.+?\\\)", re.S)

_PLACEHOLDER = "\u0000{}\u0000"

_LINK = re.compile(r"\[([^\]\n]*)\]\(([^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_AUTOLINK = re.compile(r'(?<!["\'>=/])https?://[^\s<>"\')]+[^\s<>"\').,;:!?]')

_SAFE_SCHEME = re.compile(r"^(?:https?://|mailto:|/|#)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def render_markdown(text: str) -> str:
    """Convert review-style Markdown to safe HTML."""
    if not text or not text.strip():
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped = html.escape(text, quote=False)
    escaped, store = _protect(escaped)
    rendered = _render_blocks(escaped)
    return _restore(rendered, store)


def looks_like_markdown(text: str) -> bool:
    """Whether rendering would actually change anything.

    Used to decide if the "original text" toggle is worth showing.
    """
    if not text:
        return False
    return bool(
        _CODE_SPAN.search(text)
        or _FENCE.search(text)
        or _HEADING.search(text)
        or _LIST_ITEM.search(text)
        or _QUOTE.search(text)
        or _BOLD.search(text)
        or _LINK.search(text)
        or _STRIKE.search(text)
        or _ITALIC_STAR.search(text)
        or _TABLE_SEP.search(text)
    )


# --------------------------------------------------------------------------- #
# protection of code spans and maths
# --------------------------------------------------------------------------- #
def _protect(text: str) -> tuple[str, list[str]]:
    """Lift code spans and maths out of the way, returning placeholders."""
    store: list[str] = []

    def stash(fragment: str) -> str:
        store.append(fragment)
        return _PLACEHOLDER.format(len(store) - 1)

    text = _CODE_SPAN.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), text)
    # Maths keeps its delimiters: without a typesetter the delimiters are the
    # only thing telling the reader this is a formula rather than prose.
    text = _MATH_BLOCK.sub(lambda m: stash(_math(m.group(0))), text)
    text = _MATH_INLINE.sub(lambda m: stash(_math(m.group(0))), text)
    return text, store


def _math(source: str) -> str:
    """Typeset one delimited expression (see :mod:`orcr.mathrender`)."""
    return render_math(source)


def _restore(rendered: str, store: list[str]) -> str:
    for index, fragment in enumerate(store):
        rendered = rendered.replace(_PLACEHOLDER.format(index), fragment)
    return rendered


# --------------------------------------------------------------------------- #
# inline rendering
# --------------------------------------------------------------------------- #
def _inline(text: str) -> str:
    if not text:
        return ""

    def link(match: re.Match[str]) -> str:
        label = match.group(1).strip() or match.group(2)
        return (f'<a href="{_safe_url(match.group(2))}" target="_blank" '
                f'rel="noopener">{label}</a>')

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _STRIKE.sub(r"<del>\1</del>", text)
    text = _ITALIC_STAR.sub(r"<em>\1</em>", text)
    text = _ITALIC_UNDER.sub(r"<em>\1</em>", text)
    text = _AUTOLINK.sub(
        lambda m: f'<a href="{_safe_url(m.group(0))}" target="_blank" '
                  f'rel="noopener">{m.group(0)}</a>',
        text,
    )
    return text


def _safe_url(url: str) -> str:
    """Escape a URL for an href, refusing anything but http(s)/mailto/anchor.

    The text was already HTML-escaped, so it is unescaped here and re-escaped
    with quote=True -- otherwise an ``&`` would come out as ``&amp;amp;``.
    """
    raw = html.unescape(url).strip()
    if not _SAFE_SCHEME.match(raw):
        return "#"
    return html.escape(raw, quote=True)


# --------------------------------------------------------------------------- #
# block rendering
# --------------------------------------------------------------------------- #
def _render_blocks(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            language = fence.group(2)
            body: list[str] = []
            index += 1
            while index < total and not lines[index].strip().startswith(marker):
                body.append(lines[index])
                index += 1
            index += 1  # consume the closing fence
            attribute = f' class="language-{language}"' if language else ""
            out.append(f"<pre><code{attribute}>"
                       f"{chr(10).join(body)}</code></pre>")
            continue

        heading = _HEADING.match(line)
        if heading:
            # Shift down two levels so review headings sit below the report's
            # own section headings instead of competing with them.
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if _HRULE.match(line):
            out.append("<hr>")
            index += 1
            continue

        if "|" in line and index + 1 < total and _TABLE_SEP.match(lines[index + 1]):
            table, index = _render_table(lines, index)
            out.append(table)
            continue

        if _QUOTE.match(line):
            quoted: list[str] = []
            while index < total and (_QUOTE.match(lines[index])
                                     or (quoted and lines[index].strip())):
                quoted.append(_QUOTE.sub("", lines[index]))
                index += 1
            out.append("<blockquote>"
                       f"{_render_blocks(chr(10).join(quoted))}</blockquote>")
            continue

        if _LIST_ITEM.match(line):
            items, index = _collect_list(lines, index)
            out.append(_render_list(items))
            continue

        paragraph: list[str] = []
        while index < total and lines[index].strip() and not _starts_block(lines, index):
            paragraph.append(lines[index].strip())
            index += 1
        out.append("<p>" + "<br>".join(_inline(l) for l in paragraph) + "</p>")

    return "\n".join(out)


def _starts_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _FENCE.match(line) or _HEADING.match(line) or _HRULE.match(line):
        return True
    if _QUOTE.match(line) or _LIST_ITEM.match(line) or _TABLE_SEP.match(line):
        return True
    if "|" in line and index + 1 < len(lines) and _TABLE_SEP.match(lines[index + 1]):
        return True
    return False


# --------------------------------------------------------------------------- #
# lists
# --------------------------------------------------------------------------- #
def _collect_list(lines: list[str], start: int) -> tuple[list[list], int]:
    """Gather list items as ``[indent, marker, content_lines]``."""
    items: list[list] = []
    index = start
    total = len(lines)

    while index < total:
        match = _LIST_ITEM.match(lines[index])
        if match:
            indent = len(match.group(1).expandtabs(4))
            items.append([indent, match.group(2), [match.group(3)]])
            index += 1
            continue
        if not lines[index].strip():
            # A blank line ends the list unless another item follows.
            probe = index
            while probe < total and not lines[probe].strip():
                probe += 1
            if probe < total and _LIST_ITEM.match(lines[probe]):
                index = probe
                continue
            break
        if items:
            current_indent = len(lines[index]) - len(lines[index].lstrip())
            if current_indent > items[-1][0]:
                items[-1][2].append(lines[index].strip())
                index += 1
                continue
        break
    return items, index


def _render_list(items: list[list]) -> str:
    """Render collected items, opening and closing lists on indent changes."""
    parts: list[str] = []
    stack: list[tuple[int, str]] = []

    def close_to(indent: int) -> None:
        while stack and stack[-1][0] > indent:
            parts.append(f"</{stack.pop()[1]}>")

    for indent, marker, content in items:
        tag = "ol" if marker[0].isdigit() else "ul"
        close_to(indent)
        if not stack:
            parts.append(f"<{tag}>")
            stack.append((indent, tag))
        elif indent > stack[-1][0]:
            parts.append(f"<{tag}>")
            stack.append((indent, tag))
        elif stack[-1][1] != tag:
            parts.append(f"</{stack.pop()[1]}>")
            parts.append(f"<{tag}>")
            stack.append((indent, tag))

        body = "<br>".join(_inline(line) for line in content)
        parts.append(f"<li>{body}</li>")

    while stack:
        parts.append(f"</{stack.pop()[1]}>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _render_table(lines: list[str], start: int) -> tuple[str, int]:
    header = _split_row(lines[start])
    aligns: list[str] = []
    for spec in _split_row(lines[start + 1]):
        left, right = spec.startswith(":"), spec.endswith(":")
        aligns.append("center" if left and right else
                      "right" if right else "left" if left else "")

    index = start + 2
    rows: list[list[str]] = []
    while index < len(lines) and lines[index].strip() and "|" in lines[index]:
        rows.append(_split_row(lines[index]))
        index += 1

    def style(column: int) -> str:
        align = aligns[column] if column < len(aligns) else ""
        return f' style="text-align:{align}"' if align else ""

    parts = ["<table class=\"md-table\"><thead><tr>"]
    for column, cell in enumerate(header):
        parts.append(f"<th{style(column)}>{_inline(cell)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for column in range(len(header)):
            cell = row[column] if column < len(row) else ""
            parts.append(f"<td{style(column)}>{_inline(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts), index
