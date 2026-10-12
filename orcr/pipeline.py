"""End-to-end crawl pipeline: query -> note tree -> :class:`Paper`."""

from __future__ import annotations

import time
from typing import Any, Callable

from .api import NotFound, OpenReviewClient, OpenReviewError, TokenExpired
from .extract import Paper, build_paper
from .resolve import Resolved, ResolveError, resolve


def flatten_notes(notes: list[dict], _seen: dict[str, dict] | None = None) -> list[dict]:
    """Flatten ``details.replies`` nesting into a single de-duplicated list.

    ``/notes?forum=X`` and ``/notes?forum=X&details=replies`` disagree about
    what "all the notes" means -- one returns the thread flat, the other
    returns only top-level notes with the rest nested inside. Merging both
    costs nothing and closes the gaps that would otherwise show up as a review
    with a missing rebuttal.
    """
    seen: dict[str, dict] = _seen if _seen is not None else {}
    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = str(note.get("id") or "")
        if note_id and note_id not in seen:
            seen[note_id] = note
        replies = (note.get("details") or {}).get("replies") or []
        if replies:
            flatten_notes(replies, seen)
    return list(seen.values())


def fetch_thread(client: OpenReviewClient, forum_id: str,
                 use_details: bool = True) -> list[dict]:
    """Fetch every note belonging to a forum, with a nested-replies fallback."""
    params: dict[str, Any] = {"forum": forum_id, "trash": "false"}
    if use_details:
        params["details"] = "replies"
    notes = client.notes(**params)
    merged = flatten_notes(notes)

    has_submission = any(str(n.get("id")) == forum_id for n in merged)
    if not has_submission or len(merged) <= 1:
        # Some venues reject the details parameter or return nothing useful;
        # retry flat, then merge so neither attempt loses anything.
        try:
            flat = flatten_notes(client.notes(forum=forum_id, trash="false",
                                              details=None))
            by_id = {str(n.get("id")): n for n in merged}
            for note in flat:
                by_id.setdefault(str(note.get("id")), note)
            merged = list(by_id.values())
        except OpenReviewError:
            pass
    return merged


def crawl(
    client: OpenReviewClient,
    query: str,
    venue_hint: str | None = None,
    pick: int = 0,
    min_score: float = 0.62,
    interactive: bool = False,
    use_details: bool = True,
    log: Callable[[str], None] = print,
) -> Paper:
    """Resolve one query and return a fully built :class:`Paper`."""
    log(f"→ {query}")
    resolved: Resolved = resolve(
        client, query, venue_hint=venue_hint, pick=pick,
        min_score=min_score, interactive=interactive, log=log,
    )
    log(f"  matched [{resolved.forum_id}] {resolved.title}  ({resolved.how})")

    notes = fetch_thread(client, resolved.forum_id, use_details=use_details)
    log(f"  fetched {len(notes)} notes in the thread")

    paper = build_paper(
        notes,
        resolved.forum_id,
        submission_note=resolved.submission,
        source_query=query,
        resolved_via=resolved.how,
    )
    paper.fetched_at = time.time()
    log(
        f"  parsed: {len(paper.reviews)} reviews, "
        f"{len(paper.author_responses)} author responses, "
        f"{len(paper.comments)} comments"
    )
    for warning in paper.warnings:
        log(f"  ! {warning}")
    return paper


def crawl_many(
    client: OpenReviewClient,
    queries: list[str],
    on_error: str = "warn",
    **kwargs: Any,
) -> tuple[list[Paper], list[tuple[str, str]]]:
    """Crawl several queries. Returns ``(papers, failures)``.

    One bad query out of fifty should not lose the other forty-nine, so
    failures are collected rather than raised. ``on_error="raise"`` opts back
    into fail-fast behaviour.

    An expired token is the exception to that rule: it is not a property of
    any single paper, so it propagates and lets the caller sign in and retry
    the whole batch.
    """
    log = kwargs.pop("log", print)
    papers: list[Paper] = []
    failures: list[tuple[str, str]] = []
    for index, query in enumerate(queries, start=1):
        if len(queries) > 1:
            log(f"\n[{index}/{len(queries)}]")
        try:
            papers.append(crawl(client, query, log=log, **kwargs))
        except TokenExpired:
            raise
        except (ResolveError, OpenReviewError, NotFound, ValueError) as exc:
            if on_error == "raise":
                raise
            failures.append((query, str(exc)))
            # Only the headline here -- the caller prints the full, multi-line
            # explanation once at the end.
            log(f"  ✗ 失败：{str(exc).splitlines()[0]}")
    return papers, failures
