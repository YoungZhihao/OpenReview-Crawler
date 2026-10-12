"""Where reports are written, and what they are called.

Layout produced by :func:`write_reports`::

    results/
      BadChain - Backdoor Chain-of-Thought Prompting for Large Langu/
        BadChain - Backdoor Chain-of-Thought Prompting for Largu.html
        BadChain - Backdoor Chain-of-Thought Prompting for Largu.md
        paper.json
      Direct Preference Optimization - Your Language Model is Sec/
        ...
      SUMMARY.md          (only when more than one paper was crawled)
      report.html         (only when more than one paper was crawled)

Each paper gets its own folder named after the paper, so the two report files
and their metadata stay together and a folder can be zipped or moved as a unit.
`paper.json` is not decoration: it is how a re-run recognises that the folder is
the same paper and overwrites it, instead of piling up `Title (2)` copies.

The old flat layout -- every file directly in the output directory -- is still
available with ``flat=True`` / ``--flat``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .extract import Paper
from .render_html import render_report
from .render_md import render_index, render_paper

DEFAULT_OUT_DIR = "results"

# Windows' classic path limit is 260 characters including the terminating NUL.
# Staying under it leaves room for the folder to be moved a level deeper
# without breaking. Name lengths are derived from what is actually left rather
# than from a fixed guess: a fixed 70-character cap truncated perfectly
# reasonable paper titles even when the path had plenty of room.
MAX_PATH_CHARS = 240

# Characters Windows forbids in a file name, plus control characters.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_DASHES = re.compile(r"-{2,}")

# Windows refuses these as file names, extension or not.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

MARKER_FILE = "paper.json"


def path_budget(out_dir: Path, reserve: int = 0, root: Path | None = None) -> int:
    """Characters left for one more path component under ``out_dir``."""
    base = root or Path(out_dir).resolve()
    used = len(str(base))
    return max(24, MAX_PATH_CHARS - used - reserve - 2)


def plan_names(out_dir: Path, title: str, fallback: str,
               formats: set[str]) -> tuple[str, str]:
    """``(folder_name, file_stem)`` for one paper.

    Three cases, in order of preference:

    1. the title fits on both the folder and the file -> use it for both;
    2. it fits on the folder only -> the folder keeps the complete title (that
       is the name people read in Explorer) and the file becomes ``report``,
       which is unambiguous inside a folder that already names the paper;
    3. it does not fit even on the folder -> truncate, keeping the path under
       Windows' limit.
    """
    extension = max((len(e) for e in (".html", ".md")
                     if e.lstrip(".") in formats), default=5)
    # "folder/file.ext": one separator between, one before the file.
    room = path_budget(out_dir, reserve=extension + 2)

    full = sanitize_name(title, fallback=fallback, limit=room)
    if 2 * len(full) + 1 <= room:
        return full, full

    folder = sanitize_name(title, fallback=fallback, limit=max(24, room - len("report")))
    return folder, "report"


@dataclass
class WrittenFile:
    path: Path
    kind: str  # "html" | "md" | "summary" | "report" | "meta"

    @property
    def relative(self) -> str:
        return self.path.name

    def display(self, root: Path) -> str:
        try:
            return str(self.path.relative_to(root))
        except ValueError:
            return str(self.path)


def sanitize_name(text: str, fallback: str = "paper", limit: int = 70) -> str:
    """Turn a paper title into a safe, readable file or folder name.

    Keeps CJK and other Unicode letters (Windows handles them fine and papers
    are often titled in Chinese); removes everything Windows forbids, collapses
    whitespace, and trims trailing dots or spaces, which Windows silently
    strips and which would otherwise make two names collide.
    """
    text = (text or "").strip()
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = _ILLEGAL.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.rstrip(". ")
    # A title that is only punctuation leaves nothing usable behind.
    if not text:
        return (fallback or "paper")[:limit]
    if len(text) > limit:
        truncated = text[:limit]
        # Prefer cutting at a word boundary so the name stays readable.
        if " " in truncated[limit // 2:]:
            truncated = truncated[:truncated.rfind(" ")]
        text = truncated.rstrip(". -") or text[:limit]
    if text.lower() in _RESERVED:
        text = f"{text}_paper"
    return text


def _folder_for_named(out_dir: Path, base: str, paper: Paper,
                      taken: set[Path]) -> Path:
    """Resolve the folder for a paper whose name has already been chosen."""
    candidate = out_dir / base

    marker = candidate / MARKER_FILE
    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            data = {}
        if data.get("forum_id") == paper.forum_id and candidate not in taken:
            return candidate
        # Same title, different paper (or already used in this batch).
        candidate = out_dir / f"{base} [{paper.forum_id}]"
    elif candidate.exists() and any(candidate.iterdir()):
        # A non-empty folder we did not create: never write into it blindly.
        candidate = out_dir / f"{base} [{paper.forum_id}]"

    # Final guard against two papers in one batch resolving to the same path.
    original = candidate
    suffix = 2
    while candidate in taken or (
            candidate != original and candidate.exists()
            and not (candidate / MARKER_FILE).exists()):
        candidate = original.parent / f"{original.name} ({suffix})"
        suffix += 1
    return candidate


def write_reports(
    papers: list[Paper],
    out_dir: str | Path = DEFAULT_OUT_DIR,
    formats: tuple[str, ...] | list[str] = ("md", "html"),
    include_abstract: bool = True,
    flat: bool = False,
    log=None,
) -> list[WrittenFile]:
    """Write every report and return what was written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = {f.lower() for f in formats}
    written: list[WrittenFile] = []
    taken: set[Path] = set()
    md_paths: dict[str, str] = {}

    for paper in papers:
        if flat:
            folder = out_dir
            stem = paper.forum_id
        else:
            folder_name, stem = plan_names(out_dir, paper.title, paper.forum_id,
                                           formats)
            folder = _folder_for_named(out_dir, folder_name, paper, taken)
            folder.mkdir(parents=True, exist_ok=True)
            taken.add(folder)

        if "md" in formats:
            path = folder / f"{stem}.md"
            path.write_text(render_paper(paper, include_abstract=include_abstract),
                            encoding="utf-8")
            written.append(WrittenFile(path, "md"))
            if not flat:
                md_paths[paper.forum_id] = f"{folder.name}/{path.name}"

        if "html" in formats:
            path = folder / f"{stem}.html"
            path.write_text(render_report([paper], title=paper.title),
                            encoding="utf-8")
            written.append(WrittenFile(path, "html"))

        if not flat:
            path = folder / MARKER_FILE
            path.write_text(_paper_metadata(paper, folder, formats), encoding="utf-8")
            written.append(WrittenFile(path, "meta"))

    if len(papers) > 1:
        if "md" in formats:
            path = out_dir / "SUMMARY.md"
            path.write_text(render_index(papers, md_paths=md_paths), encoding="utf-8")
            written.append(WrittenFile(path, "summary"))
        if "html" in formats:
            path = out_dir / "report.html"
            path.write_text(render_report(papers), encoding="utf-8")
            written.append(WrittenFile(path, "report"))

    if log:
        log(f"输出目录：{out_dir.resolve()}")
    return written


def _paper_metadata(paper: Paper, folder: Path, formats: set[str]) -> str:
    """Small sidecar describing the folder's contents.

    Doubles as the identity record that lets a later run tell "this folder is
    the same paper, overwrite it" from "this folder is a different paper that
    happens to share a title".
    """
    stem = folder.name
    files = {}
    if "html" in formats:
        files["html"] = f"{stem}.html"
    if "md" in formats:
        files["md"] = f"{stem}.md"
    payload = {
        "forum_id": paper.forum_id,
        "title": paper.title,
        "venue": paper.venue,
        "venue_raw": paper.venue_raw,
        "url": paper.forum_url,
        "pdf": paper.pdf_url,
        "authors": paper.authors,
        "keywords": paper.keywords,
        "reviews": len(paper.reviews),
        "author_responses": len(paper.author_responses),
        "comments": len(paper.comments),
        "ratings": paper.ratings,
        "mean_rating": paper.mean_rating,
        "decision": paper.decision_text,
        "withdrawn": paper.withdrawn,
        "fetched_at": datetime.fromtimestamp(
            paper.fetched_at or time.time()).isoformat(timespec="seconds"),
        "source_query": paper.source_query,
        "resolved_via": paper.resolved_via,
        "files": files,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
