"""Input resolution: turn whatever the user pasted into an OpenReview forum id.

Accepted inputs, in the order they are tried:

1. An OpenReview URL -- ``/forum?id=``, ``/pdf?id=``, ``/notes?forum=``, ...
2. A bare OpenReview note id (10-ish base64url characters).
3. An arXiv id or ``arxiv.org/abs/...`` URL -- the title is read from the arXiv
   API and then matched against OpenReview.
4. A DOI or ``doi.org/...`` URL -- likewise, via Crossref.
5. Anything else is treated as a paper title and searched on OpenReview.

Steps 3 and 4 exist because people rarely have the OpenReview link to hand;
they have the arXiv link. Both are best-effort: if the metadata service is
unreachable the query still works as a plain title search.
"""

from __future__ import annotations

import difflib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from .api import NotFound, OpenReviewClient, OpenReviewError, unwrap_content
from .extract import is_bibliographic_record, is_workshop, linked_forum_id

OPENREVIEW_HOSTS = ("openreview.net",)
_ID_IN_URL = re.compile(r"[?&](?:id|forum|noteId)=([A-Za-z0-9_\-]+)")
_BARE_ID = re.compile(r"^[A-Za-z0-9_\-]{6,24}$")
_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_ARXIV_OLD = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)")


class ResolveError(RuntimeError):
    """Raised when no confident match could be found."""


@dataclass
class Candidate:
    forum_id: str
    title: str
    venue: str
    score: float
    url: str = ""

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.score:5.2f}  {self.venue or '?':<34} {self.title[:70]}"


@dataclass
class Resolved:
    forum_id: str
    title: str
    venue: str
    submission: dict[str, Any]
    candidates: list[Candidate] = field(default_factory=list)
    how: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"https://openreview.net/forum?id={self.forum_id}"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def normalize_title(text: str) -> str:
    """Lowercase, drop LaTeX/punctuation, collapse whitespace -- for matching."""
    text = text or ""
    text = re.sub(r"\$[^$]*\$", " ", text)
    text = re.sub(r"\\[a-zA-Z]+", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def title_similarity(query: str, candidate: str) -> float:
    """Blend of character-level and token-level similarity, 0..1."""
    a, b = normalize_title(query), normalize_title(candidate)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    contained = 1.0 if (a in b or b in a) else 0.0
    return max(ratio, 0.5 * ratio + 0.5 * jaccard, 0.85 * contained + 0.15 * ratio)


def _http_json(url: str, timeout: float = 20) -> Any:
    import json

    request = urllib.request.Request(
        url, headers={"User-Agent": "openreview-crawler/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_text(url: str, timeout: float = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "openreview-crawler/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def arxiv_title(arxiv_id: str) -> str | None:
    """Look up a paper title on arXiv. Returns None when unavailable."""
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id.strip())
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"id_list": arxiv_id, "max_results": 1}
    )
    try:
        xml = _http_text(url)
        root = ET.fromstring(xml)
    except (urllib.error.URLError, ET.ParseError, OSError, ValueError):
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        return None
    node = entry.find("a:title", ns)
    if node is None or not node.text:
        return None
    return " ".join(node.text.split())


def crossref_title(doi: str) -> str | None:
    """Look up a paper title by DOI via Crossref. Returns None when unavailable."""
    doi = doi.strip().rstrip(".,;)")
    # arXiv DOIs are just 10.48550/arXiv.<id> -- go straight to the arXiv API.
    match = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.IGNORECASE)
    if match:
        return arxiv_title(match.group(1))
    try:
        data = _http_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    message = (data or {}).get("message") or {}
    titles = message.get("title") or []
    return titles[0] if titles else None


def parse_query(query: str) -> dict[str, str | None]:
    """Classify a raw input string."""
    # Strip a stray BOM: it survives copy-paste from editors and would defeat
    # every prefix check below.
    text = (query or "").replace("\ufeff", "").strip().strip('"').strip("'")
    if not text:
        raise ResolveError("empty input")

    lowered = text.lower()

    # 1. OpenReview URL (or any URL carrying an id= parameter).
    if any(host in lowered for host in OPENREVIEW_HOSTS):
        match = _ID_IN_URL.search(text)
        if match:
            return {"kind": "forum_id", "value": match.group(1), "raw": text}
        raise ResolveError(
            f"OpenReview URL without an id parameter: {text}\n"
            "Expected something like https://openreview.net/forum?id=XXXX"
        )

    # 2. arXiv URL or id.
    if "arxiv.org" in lowered:
        tail = text.rstrip("/").split("/")[-1]
        match = _ARXIV_NEW.search(tail) or _ARXIV_OLD.search(tail)
        if match:
            return {"kind": "arxiv", "value": match.group(1), "raw": text}
    match = _ARXIV_NEW.fullmatch(text) or _ARXIV_OLD.fullmatch(text)
    if match:
        return {"kind": "arxiv", "value": match.group(1), "raw": text}
    if lowered.startswith("arxiv:"):
        return {"kind": "arxiv", "value": text.split(":", 1)[1].strip(), "raw": text}

    # 3. DOI.
    if "doi.org" in lowered or lowered.startswith("doi:"):
        candidate = text.split("doi.org/")[-1] if "doi.org" in lowered else text[4:]
        match = _DOI.search(candidate)
        if match:
            return {"kind": "doi", "value": match.group(1), "raw": text}
    match = _DOI.search(text)
    if match and " " not in text.strip():
        return {"kind": "doi", "value": match.group(1), "raw": text}

    # 4. Bare OpenReview note id (no spaces, id-shaped).
    if _BARE_ID.match(text) and " " not in text:
        return {"kind": "maybe_id", "value": text, "raw": text}

    # 5. Everything else: a title.
    return {"kind": "title", "value": text, "raw": text}


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _submission_summary(note: dict) -> tuple[str, str]:
    content = unwrap_content(note.get("content") or {})
    title = str(content.get("title") or "").strip()
    venue = str(content.get("venue") or content.get("venueid") or "").strip()
    return title, venue


# Bibliographic-record detection lives in :mod:`orcr.extract`
# (``is_bibliographic_record``). The venue *string* is not a reliable signal --
# a DBLP mirror of an ICLR paper carries the clean label "ICLR 2024" -- so the
# check uses the invitation and venue id instead.


def follow_bibliographic(
    client: OpenReviewClient,
    root: dict,
    forum_id: str,
    log=None,
    max_hops: int = 3,
) -> tuple[dict, str]:
    """Follow a DBLP/Crossref mirror to the real submission it points at.

    A mirrored record and the real submission can share a title and even a
    venue label, but only the real one has reviews. The mirror's ``html`` field
    holds the canonical forum URL, so the fix is to notice we landed on a
    record and hop to the paper instead of returning an empty report.
    """
    seen = {forum_id}
    for _ in range(max_hops):
        if not is_bibliographic_record(root):
            break
        target = linked_forum_id(root)
        if not target or target in seen:
            if log:
                log(f"  {forum_id} 是书目记录，但它没有指向任何 OpenReview 投稿")
            break
        if log:
            log(f"  {forum_id} 是书目记录（没有评审），跟随链接到真实投稿 {target}")
        seen.add(target)
        try:
            notes = client.notes(forum=target)
        except (OpenReviewError, NotFound):
            break
        new_root = next((n for n in notes if str(n.get("id")) == target), None)
        if new_root is None:
            break
        root, forum_id = new_root, target
    return root, forum_id


def _candidates_from_notes(notes: list[dict], query_title: str,
                           venue_hint: str | None) -> list[Candidate]:
    hint = (venue_hint or "").lower()
    out: list[Candidate] = []
    for note in notes:
        title, venue = _submission_summary(note)
        if not title:
            continue
        content = unwrap_content(note.get("content") or {})
        venueid = str(content.get("venueid") or "")
        score = title_similarity(query_title, title)

        # A mirrored record has no reviews behind it, so it must never outrank
        # a real submission that happens to share the title.
        if is_bibliographic_record(note):
            score *= 0.70
        # The same paper is often submitted to both a workshop and the main
        # track; prefer the main track unless the caller asked for a workshop.
        if is_workshop(venueid, venue) and "workshop" not in hint:
            score *= 0.94
        if venue_hint:
            if venue_hint.lower() in venue.lower():
                score += 0.12
            else:
                score -= 0.25
        out.append(
            Candidate(
                forum_id=str(note.get("forum") or note.get("id")),
                title=title,
                venue=venue,
                score=score,
                url=f"https://openreview.net/forum?id={note.get('forum') or note.get('id')}",
            )
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def _search_once(client: OpenReviewClient, term: str, limit: int,
                 log=None, **params: Any) -> list[dict]:
    """One search attempt, retrying an empty or failed answer once.

    ``/notes/search`` intermittently answers ``count=0`` for a query it handled
    a second earlier, and the failure mode of the human-check challenge is
    indistinguishable from "no results" unless the error is inspected. So an
    empty result is retried once before being believed, and the reason for a
    failure is reported rather than swallowed.
    """
    for attempt in range(2):
        try:
            notes = client.search_notes(term=term, limit=limit, **params)
        except OpenReviewError as exc:
            if log and attempt == 0:
                log(f"  search attempt failed ({params or 'default params'}): "
                    f"{str(exc).splitlines()[0]}")
            notes = []
        if notes:
            return notes
        if attempt == 0:
            time.sleep(1.2)
    return []


def _shorten(title: str, words: int = 9) -> str:
    parts = title.split()
    return " ".join(parts[:words]) if len(parts) > words else title


def search_papers(client: OpenReviewClient, title: str, venue_hint: str | None = None,
                  limit: int = 40, log=None) -> list[Candidate]:
    """Search OpenReview by title and rank the submission notes by similarity.

    Three parameter sets are tried, best first, because each covers a case the
    others miss:

    ``content=title``
        Matches only the title field and returns submission notes directly, so
        it gives an exact hit without further requests. This is the primary
        strategy.
    ``content=all`` + ``source=forum``
        Broader, catches papers whose stored title differs slightly from the
        query. Intermittently empty.
    no filter at all
        Matches review and comment bodies too. Hits carry no title, but they do
        carry a ``forum`` id, which is enough to look the submission up.
    """
    found: dict[str, dict] = {}

    def absorb(notes: list[dict]) -> None:
        for note in notes:
            key = str(note.get("forum") or note.get("id") or "")
            if key:
                found.setdefault(key, note)

    def has_title(note: dict) -> bool:
        content = unwrap_content(note.get("content") or {})
        return bool(str(content.get("title") or "").strip())

    queries = [title]
    short = _shorten(title)
    if short != title:
        queries.append(short)

    for term in queries:
        absorb(_search_once(client, term, limit, log=log, content="title"))
        if len([n for n in found.values() if has_title(n)]) >= 3:
            break
        absorb(_search_once(client, term, limit, log=log, content="all",
                            group="all", source="forum"))
        if len([n for n in found.values() if has_title(n)]) >= 3:
            break

    titled = [n for n in found.values() if has_title(n)]

    # Last resort: search everything, then look each forum up by id. Hits from
    # review bodies have no title, so `/notes?id=<forum>` is the only way to
    # find out what paper they belong to.
    if not titled:
        if log:
            log("  title search came back empty; falling back to full-text search")
        loose: list[dict] = []
        for term in queries:
            loose = _search_once(client, term, limit, log=log)
            if loose:
                break
        forum_ids: list[str] = []
        for note in loose:
            forum_id = str(note.get("forum") or note.get("id") or "")
            if forum_id and forum_id not in forum_ids:
                forum_ids.append(forum_id)
        for forum_id in forum_ids[:12]:
            try:
                root = client.note(forum_id)
            except (OpenReviewError, NotFound):
                continue
            if root and str(root.get("id")) == forum_id:
                found.setdefault(forum_id, root)
        titled = [n for n in found.values() if has_title(n)]

    return _candidates_from_notes(titled, title, venue_hint)


def resolve(
    client: OpenReviewClient,
    query: str,
    venue_hint: str | None = None,
    pick: int = 0,
    min_score: float = 0.62,
    interactive: bool = False,
    log=print,
) -> Resolved:
    """Resolve one user input into a forum id plus its submission note."""
    parsed = parse_query(query)
    kind, value = parsed["kind"], parsed["value"]
    assert value is not None

    # -- direct id -------------------------------------------------------- #
    if kind in ("forum_id", "maybe_id"):
        notes = client.notes(forum=value)
        if notes:
            root = next((n for n in notes if n.get("id") == value), None)
            if root is None:
                root = next(
                    (n for n in notes if str(n.get("forum")) == value and not n.get("replyto")),
                    notes[0],
                )
            forum_id = str(root.get("forum") or root.get("id") or value)
            # The user may have pasted a DBLP/arXiv mirror link rather than the
            # real submission -- hop to the paper that actually has reviews.
            root, forum_id = follow_bibliographic(client, root, forum_id, log=log)
            title, venue = _submission_summary(root)
            return Resolved(
                forum_id=forum_id,
                title=title or f"(untitled {forum_id})",
                venue=venue,
                submission=root,
                how=f"direct id {value}",
            )
        if kind == "forum_id":
            raise ResolveError(
                f"No notes found for id {value}. It may be private (retry with --token) "
                "or the link may be malformed."
            )
        log(f"  '{value}' is not a known note id -- searching it as a title instead.")
        title_query = value
    else:
        title_query = value
        if kind == "arxiv":
            found = arxiv_title(value)
            if found:
                log(f"  arXiv {value} -> {found!r}")
                title_query = found
            else:
                log(f"  arXiv lookup failed for {value}; searching the raw string.")
        elif kind == "doi":
            found = crossref_title(value)
            if found:
                log(f"  DOI {value} -> {found!r}")
                title_query = found
            else:
                log(f"  Crossref lookup failed for {value}; searching the raw string.")

    # -- title search ----------------------------------------------------- #
    log(f"  searching OpenReview for: {title_query[:90]}")
    candidates = search_papers(client, title_query, venue_hint, log=log)
    if not candidates:
        raise ResolveError(
            f"No OpenReview submission matched {title_query!r}. "
            "Try the exact paper title, the forum URL, or lower --min-score."
        )

    best = candidates[0]
    if best.score < min_score:
        listing = "\n".join(f"    {c}" for c in candidates[:8])
        raise ResolveError(
            f"No confident match for {title_query!r} (best score {best.score:.2f} "
            f"< {min_score:.2f}). Closest candidates:\n{listing}\n"
            "Re-run with the forum URL, or pass --min-score with a lower value, "
            "or --pick N to force a candidate."
        )

    if interactive and len(candidates) > 1 and candidates[1].score > best.score - 0.08:
        print("\nMultiple plausible matches:")
        for i, cand in enumerate(candidates[:8]):
            print(f"  [{i}] {cand}")
        try:
            raw = input("Pick a number (Enter for 0): ").strip()
            pick = int(raw) if raw else 0
        except (EOFError, ValueError):
            pick = 0

    chosen = candidates[pick] if 0 <= pick < len(candidates) else best
    notes = client.notes(forum=chosen.forum_id)
    root = next(
        (n for n in notes if str(n.get("id")) == chosen.forum_id),
        notes[0] if notes else None,
    )
    if root is None:
        raise ResolveError(f"Forum {chosen.forum_id} returned no notes.")

    forum_id = str(root.get("forum") or root.get("id") or chosen.forum_id)
    root, forum_id = follow_bibliographic(client, root, forum_id, log=log)

    title, venue = _submission_summary(root)
    return Resolved(
        forum_id=forum_id,
        title=title or chosen.title,
        venue=venue or chosen.venue,
        submission=root,
        candidates=candidates[:10],
        how=f"title search (score {chosen.score:.2f})",
    )
