"""Turn a flat list of OpenReview notes into a structured, renderable paper.

The API hands back every note in a forum as one flat, unordered list keyed by
``replyto``. Almost all of the work -- and almost all of the ways this can go
wrong -- lives in rebuilding that into threads and labelling who said what.

The important rules encoded here:

* Notes come in two shapes. api2 venues wrap content as ``{"value": X}``; older
  venues still store bare values. :func:`orcr.api.unwrap` handles both, and
  nothing in this module touches ``content`` directly.
* ``invitation`` may be a string (v1) or ``invitations`` a list (v2). The kind
  is derived from the *last* path segment after ``/-/``, because the same
  invitation names recur across every venue and year.
* Reviewer identity is normally anonymous, so a reviewer is identified by the
  signature tail (``Reviewer_abc``), not by a profile. When a venue does *not*
  hide identities the signature is a ``~Profile_Id1`` and we surface it.
* Threads are reconstructed recursively rather than assumed to be one level
  deep: author rebuttals often sit under a review, and reviewer follow-ups sit
  under the rebuttal.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .api import as_text, unwrap_content

# --------------------------------------------------------------------------- #
# text cleaning
# --------------------------------------------------------------------------- #
_TAG_BR = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG_BLOCK = re.compile(r"</\s*(p|div|li|tr)\s*>", re.IGNORECASE)
_TAG_ANY = re.compile(r"<[^>]+>")


def clean_text(value: Any) -> str:
    """Normalise a raw field value into readable plain text.

    Reviews are stored as loose HTML. We keep the paragraph and line breaks
    (they carry meaning) and drop everything else. LaTeX is deliberately left
    untouched -- ``$x$`` and ``\\(x\\)`` are rendered by whatever the reader
    uses, and mangling them would be worse than leaving them raw.
    """
    text = as_text(value)
    if not text:
        return ""
    text = _TAG_BR.sub("\n", text)
    text = _TAG_BLOCK.sub("\n\n", text)
    text = _TAG_ANY.sub("", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# note classification
# --------------------------------------------------------------------------- #
KIND_LABELS = {
    "submission": "Submission",
    "review": "Official Review",
    "meta_review": "Meta Review",
    "decision": "Decision",
    "author_response": "Author Response",
    "comment": "Comment",
    "withdrawal": "Withdrawal",
    "desk_reject": "Desk Reject",
    "other": "Note",
}

KIND_ZH = {
    "submission": "投稿",
    "review": "审稿意见",
    "meta_review": "元审稿意见",
    "decision": "最终决定",
    "author_response": "作者回复",
    "comment": "讨论评论",
    "withdrawal": "撤稿",
    "desk_reject": "拒稿",
    "other": "其他",
}

# Ordered longest-first: "Meta_Review" must win over "Review".
_KIND_RULES: list[tuple[str, str]] = [
    ("meta_review", "meta_review"),
    ("metareview", "meta_review"),
    ("official_review", "review"),
    ("review", "review"),
    ("author_rebuttal", "author_response"),
    ("author_response", "author_response"),
    ("author_comment", "author_response"),
    ("author_final_remarks", "author_response"),
    ("rebuttal", "author_response"),
    ("response", "author_response"),
    ("decision", "decision"),
    ("withdrawal", "withdrawal"),
    ("desk_reject", "desk_reject"),
    ("desk_rejection", "desk_reject"),
    ("official_comment", "comment"),
    ("public_comment", "comment"),
    # Reviewer checklist affirmations (NeurIPS 2025 posts one per review).
    # They are acknowledgements, not discussion, and are kept out of threads.
    ("mandatory_acknowledgement", "acknowledgement"),
    ("acknowledgement", "acknowledgement"),
    ("acknowledgment", "acknowledgement"),
    ("comment", "comment"),
    ("submission", "submission"),
]

# Kinds that carry no discussion content and are left out of rendered threads.
NON_CONTENT_KINDS = {"acknowledgement"}


def invitation_ids(note: dict) -> list[str]:
    """All invitation ids on a note, tolerating v1 and v2 shapes."""
    out: list[str] = []
    invitations = note.get("invitations")
    if isinstance(invitations, str):
        out.append(invitations)
    elif isinstance(invitations, (list, tuple)):
        out.extend(str(i) for i in invitations)
    single = note.get("invitation")
    if isinstance(single, str):
        out.append(single)
    return out


def classify(note: dict, forum_id: str | None = None) -> str:
    """Best-effort kind for a note.

    The invitation alone is not enough. ICLR 2024 (and ARR-style venues) post
    the authors' rebuttal as an ``Official_Comment`` rather than a
    ``Rebuttal``, so a comment signed by the ``Authors`` group is really an
    author response -- and mislabelling it as generic discussion would hide
    every rebuttal from the report.
    """
    if forum_id is not None and str(note.get("id")) == str(forum_id):
        return "submission"
    if not note.get("replyto") and not note.get("forum"):
        return "submission"

    kind = ""
    for invitation in invitation_ids(note):
        tail = invitation.rsplit("/-/", 1)[-1].lower()
        for needle, mapped in _KIND_RULES:
            if tail == needle or tail.endswith(needle):
                kind = mapped
                break
        if kind:
            break

    if not kind:
        # Fall back to the fields present: an ICLR review always has weaknesses.
        keys = {k.lower() for k in (note.get("content") or {})}
        if {"summary", "strengths", "weaknesses"} & keys:
            kind = "review" if "weaknesses" in keys else "comment"
        elif "decision" in keys:
            kind = "decision"
        elif "recommendation" in keys and "metareview" in keys:
            kind = "meta_review"
        else:
            kind = "comment"

    # Who signed it decides how a generic comment should be read.
    role, _, _ = parse_signature(primary_signature(note))
    if kind == "comment" and role == "Authors":
        return "author_response"
    return kind


# --------------------------------------------------------------------------- #
# signatures / roles
# --------------------------------------------------------------------------- #
_ROLE_PREFIXES: list[tuple[str, str]] = [
    ("Senior_Area_Chair", "Senior Area Chair"),
    ("Emergency_Reviewer", "Emergency Reviewer"),
    ("Publication_Chair", "Publication Chair"),
    ("Program_Chair", "Program Chair"),
    ("Ethics_Reviewer", "Ethics Reviewer"),
    ("Meta_Reviewer", "Meta Reviewer"),
    ("Area_Chair", "Area Chair"),
    ("Action_Editor", "Action Editor"),
    ("Authors", "Authors"),
    ("Author", "Authors"),
    ("Reviewer", "Reviewer"),
    ("Reviewers", "Reviewer"),
    ("AC", "Area Chair"),
    ("SAC", "Senior Area Chair"),
    ("PC", "Program Chair"),
]


def parse_signature(signature: str) -> tuple[str, str, str]:
    """Split a signature into ``(role, anon_suffix, profile_id)``.

    ``ICLR.cc/2024/Conference/Submission123/Reviewer_abc`` -> ("Reviewer", "abc", "")
    ``~Jane_Doe1``                                         -> ("", "", "~Jane_Doe1")
    ``ICLR.cc/2024/Conference/Submission123/Authors``      -> ("Authors", "", "")
    """
    signature = (signature or "").strip()
    if not signature:
        return "", "", ""
    if signature.startswith("~"):
        return "", "", signature

    segment = signature.rstrip("/").rsplit("/", 1)[-1]
    for prefix, role in _ROLE_PREFIXES:
        if segment == prefix:
            return role, "", ""
        if segment.startswith(prefix + "_"):
            suffix = segment[len(prefix) + 1:]
            return role, suffix, ""
    return segment.replace("_", " "), "", ""


def primary_signature(note: dict) -> str:
    signatures = note.get("signatures") or []
    if isinstance(signatures, str):
        return signatures
    for sig in signatures:
        if isinstance(sig, str):
            return sig
    return ""


# --------------------------------------------------------------------------- #
# bibliographic records
# --------------------------------------------------------------------------- #
# OpenReview mirrors DBLP/Crossref/arXiv metadata into the same search index.
# A mirrored record looks like a legitimate paper -- real title, real authors,
# a clean "ICLR 2024" venue string -- but it has no reviews behind it, and a
# title search will happily return it as the best match.
#
# The only dependable tell is the invitation / venue id. Note that real
# submissions also carry `_bibtex` and `paperhash`, so those fields say nothing
# about whether a record is a mirror; using them would demote the right answer.
BIBLIOGRAPHIC_INVITATION_PREFIXES = ("DBLP.org/", "Crossref/", "CoRR/")
BIBLIOGRAPHIC_VENUEID_MARKERS = ("dblp.org", "crossref.org", "doi.org")

_FORUM_LINK = re.compile(r"[?&]id=([A-Za-z0-9_\-]+)")


def is_bibliographic_record(note: dict) -> bool:
    """True when a note is imported metadata rather than a real submission."""
    for invitation in invitation_ids(note):
        if invitation.startswith(BIBLIOGRAPHIC_INVITATION_PREFIXES):
            return True
    content = unwrap_content(note.get("content") or {})
    venueid = str(content.get("venueid") or "").lower()
    return any(marker in venueid for marker in BIBLIOGRAPHIC_VENUEID_MARKERS)


def linked_forum_id(note: dict) -> str:
    """The real OpenReview forum a bibliographic record points at, if any.

    DBLP imports keep the canonical URL in ``html``; that field is the bridge
    from a useless search hit to the submission thread with the reviews.
    """
    content = unwrap_content(note.get("content") or {})
    for key in ("html", "url", "pdf", "forum"):
        value = str(content.get(key) or "")
        match = _FORUM_LINK.search(value)
        if match:
            return match.group(1)
    return ""


def is_workshop(venueid: str, venue: str = "") -> bool:
    """Workshop submissions share a title with the main-track paper."""
    haystack = f"{venueid} {venue}".lower()
    return "/workshop" in haystack or "workshop" in haystack


# --------------------------------------------------------------------------- #
# field ordering
# --------------------------------------------------------------------------- #
# Ordered most-meaningful-first, across the field names actually used by ICLR,
# NeurIPS, ICML, ARR/ACL and the various TMLR-style venues.
#
# Order matters a lot in practice: NeurIPS 2025 puts the entire body of the
# review in a single `strengths_and_weaknesses` field, so leaving it out of this
# list dropped the substantive content below every numeric sub-score while the
# checklist acknowledgements sat above it.
REVIEW_FIELDS: list[tuple[str, str]] = [
    ("summary", "总评 Summary"),
    ("paper_summary", "总评 Summary"),
    ("paper_topic_and_main_contributions", "论文主题与主要贡献"),
    ("strengths", "优点 Strengths"),
    ("weaknesses", "缺点 Weaknesses"),
    ("strengths_and_weaknesses", "优点与缺点 Strengths & Weaknesses"),
    ("reasons_to_accept", "接受理由 Reasons to Accept"),
    ("reasons_to_reject", "拒稿理由 Reasons to Reject"),
    ("questions", "向作者提出的问题 Questions"),
    ("questions_for_the_authors", "向作者提出的问题 Questions"),
    ("key_questions_for_authors", "向作者提出的问题 Questions"),
    ("limitations", "局限性 Limitations"),
    ("missing_references", "缺失的参考文献"),
    ("soundness", "Soundness 技术可靠性"),
    ("presentation", "Presentation 表达"),
    ("contribution", "Contribution 贡献"),
    ("technical_novelty_and_significance", "Technical Novelty & Significance"),
    ("empirical_novelty_and_significance", "Empirical Novelty & Significance"),
    ("originality", "Originality 原创性"),
    ("clarity", "Clarity 清晰度"),
    ("quality", "Quality 质量"),
    ("significance", "Significance 重要性"),
    ("excitement", "Excitement 兴奋度"),
    ("reproducibility", "Reproducibility 可复现性"),
    ("code_of_ethics", "Code of Ethics"),
    ("flag_for_ethics_review", "Ethics Review Flag"),
    ("ethical_concerns", "伦理关切"),
    ("review", "审稿全文 Review"),
    ("comment", "补充评论 Comment"),
    # The reviewer's closing rationale reads best after the substance.
    ("final_justification", "最终理由 Final Justification"),
]

META_FIELDS: list[tuple[str, str]] = [
    ("metareview", "元审稿全文 Meta Review"),
    ("summary", "总评 Summary"),
    ("strengths", "优点 Strengths"),
    ("weaknesses", "缺点 Weaknesses"),
    ("strengths_and_weaknesses", "优点与缺点 Strengths & Weaknesses"),
    ("recommendation", "推荐意见 Recommendation"),
    ("confidence", "置信度 Confidence"),
    ("comment", "补充评论 Comment"),
]

DECISION_FIELDS: list[tuple[str, str]] = [
    ("decision", "决定 Decision"),
    ("recommendation", "推荐意见 Recommendation"),
    ("comment", "说明 Comment"),
]

SCORE_FIELDS = ("rating", "confidence", "recommendation", "soundness",
                "presentation", "contribution", "overall_assessment")

# Field names differ per venue, and *casing* differs too: EMNLP/ACL reviews use
# "Questions_for_the_Authors", "Reviewer_Confidence", "Excitement" while ICLR
# uses lower-case names. Every lookup therefore goes through field_key(), which
# compares case-insensitively.
RATING_FIELD_NAMES = (
    "rating", "overall_recommendation", "overall_assessment", "recommendation",
    "reviewer_rating", "score", "final_rating",
    # EMNLP/ACL 2023 reviews carry no overall rating; "Excitement" (1-5) is
    # their score, formatted exactly like a rating ("3: Ambivalent: ...").
    "excitement",
)
CONFIDENCE_FIELD_NAMES = (
    "confidence", "reviewer_confidence", "reviewer_expertise",
)
QUESTION_FIELD_NAMES = (
    "questions", "questions_for_the_authors", "key_questions_for_authors",
    "questions_to_authors", "author_questions", "question",
)

# Bookkeeping keys that are never worth showing as review content.
_NOISE_KEYS = {
    "venue", "venueid", "pdf", "html", "code", "supplementary_material",
    "paperhash", "tldr", "keywords", "authors", "authorids", "title",
    "abstract", "one_sentence_summary", "primary_area", "TLDR", "TLDR_",
    "_bibtex",
}

# Checklist affirmations and policy confirmations. They are booleans dressed as
# prose ("Yes", "Affirmed."), carry no review content, and on NeurIPS and
# ICML 2026 submissions they otherwise sit near the top of every review.
_ACKNOWLEDGEMENT_KEYS = {
    "code_of_conduct",
    "code_of_conduct_acknowledgement",
    "responsible_reviewing_acknowledgement",
    "mandatory_acknowledgement",
    "rebuttal_acknowledgement",
    "acknowledgement",
    "acknowledgements",
    "acknowledgment",
    "acknowledgments",
    "compliance_with_llm_reviewing_policy",
    "llm_reviewing_policy",
    "details_of_ethics_concerns",
    "paper_formatting_concerns",
}

# Values that mean "the reviewer left this blank". Showing a section whose body
# is "NA" is worse than showing nothing, and a field like this must never be
# mistaken for a real question.
_PLACEHOLDER_VALUES = {
    "na", "n/a", "n.a.", "none", "nil", "null", "nan", "-", "--", "---",
    "no comments", "nothing", "not applicable", "无", "无。", "暂无", "没有",
}

# A reviewer may fill the questions field with a pointer instead of questions
# ("I have listed the questions in the weakness part, so you can refer to it").
# When that happens the real questions have to come from the body.
_POINTER_PHRASES = (
    "see the weakness", "see weakness", "see the strengths", "refer to the weakness",
    "listed in the weakness", "listed in weakness", "in the weakness part",
    "in the weaknesses part", "see above", "see below", "refer to above",
    "refer to below", "见上文", "见下文", "见缺点", "详见",
)

# Verbs that make a short, list-free, question-mark-free value a pointer rather
# than a question.
_POINTER_VERBS = (
    "see ", "refer", "listed in", "as above", "as below", "mentioned above",
    "given above", "stated above", "见", "详见", "同上",
)


def field_key(fields: dict[str, str], *names: str) -> str | None:
    """The actual key in ``fields`` matching any of ``names``, ignoring case."""
    for name in names:
        if name in fields:
            return name
    lowered = {key.lower(): key for key in fields}
    for name in names:
        found = lowered.get(name.lower())
        if found is not None:
            return found
    return None


def field_value(fields: dict[str, str], *names: str) -> str:
    """Value of the first matching field, case-insensitively, or ''."""
    key = field_key(fields, *names)
    return fields.get(key, "") if key else ""


def is_placeholder(value: str) -> bool:
    """Whether a field's value is an empty-sounding placeholder."""
    text = (value or "").strip().strip("。.!！ ").lower()
    return text in _PLACEHOLDER_VALUES


def looks_like_pointer(value: str) -> bool:
    """Whether a questions field points elsewhere instead of asking anything.

    Examples seen in the wild: "I have listed the questions in the weakness
    part, so you can refer to it." and "see above". Such a value is not a
    question, and treating it as one produces "Q1. see above".

    Being too eager here is expensive: an earlier version also matched any
    short question-mark-free text, which swallowed a reviewer's real bulleted
    question list ("- I think the most critical question would be the KL one").
    So a list is never a pointer, and neither is anything that actually asks
    something.
    """
    text = (value or "").strip()
    if not text:
        return False
    lowered = text.lower()

    if any(phrase in lowered for phrase in _POINTER_PHRASES):
        return True
    # A list is never a pointer, however it is punctuated.
    if "\n" in text or _ORDINAL_MARKER.match(text):
        return False
    if len(text) >= 200 or "?" in text or "？" in text:
        return False
    return any(verb in lowered for verb in _POINTER_VERBS)


FIELD_LABELS = dict(REVIEW_FIELDS) | dict(META_FIELDS) | dict(DECISION_FIELDS)
FIELD_LABELS.update({
    "rating": "评分 Rating",
    "confidence": "置信度 Confidence",
    "recommendation": "推荐意见 Recommendation",
    "overall_assessment": "总体评价 Overall",
    "expected_reviewer_expertise": "期望审稿人专业度",
    "justification": "理由 Justification",
    "bias": "Bias",
    "ethical_concerns": "伦理关切",
    "paper_formatting_concerns": "格式问题",
    "needs_ethics_review": "需要伦理审查",
    "rebuttal": "作者回复 Rebuttal",
    "response": "回复 Response",
    "final_decision": "最终决定",
    "author_final_remarks": "作者最后说明",
    "mandatory_acknowledgement": "审稿人确认声明",
})


def humanize(key: str) -> str:
    """``technical_novelty`` -> ``Technical Novelty``."""
    return " ".join(part.capitalize() for part in re.split(r"[_\s]+", key) if part)


def parse_score(raw: Any) -> tuple[float | None, str]:
    """``"6: Marginally above acceptance threshold"`` -> ``(6.0, "Marginally...")``."""
    text = clean_text(raw)
    if not text:
        return None, ""
    match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[:.\-–—]?\s*(.*)$", text)
    if not match:
        return None, text
    try:
        return float(match.group(1)), match.group(2).strip()
    except ValueError:
        return None, text


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class Message:
    id: str
    kind: str
    role: str
    signature: str
    anon_id: str
    profile_id: str
    created: int
    modified: int
    fields: dict[str, str]
    anon_group: str = ""
    label: str = ""
    children: list["Message"] = field(default_factory=list)
    depth: int = 0
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- convenience ------------------------------------------------------ #
    @property
    def created_dt(self) -> datetime | None:
        return to_datetime(self.created)

    @property
    def created_str(self) -> str:
        dt = self.created_dt
        return dt.strftime("%Y-%m-%d %H:%M") if dt else ""

    @property
    def role_zh(self) -> str:
        return {
            "Reviewer": "审稿人",
            "Authors": "作者",
            "Area Chair": "领域主席",
            "Senior Area Chair": "高级领域主席",
            "Program Chair": "程序主席",
            "Ethics Reviewer": "伦理审稿人",
        }.get(self.role, self.role or "匿名用户")

    def field(self, *names: str) -> str:
        """First non-empty value among ``names``, matched case-insensitively."""
        return field_value(self.fields, *names)

    def rating(self) -> tuple[float | None, str]:
        """Parsed numeric score and its meaning, across venue field names."""
        value = field_value(self.fields, *RATING_FIELD_NAMES)
        return parse_score(value) if value else (None, "")

    def confidence(self) -> tuple[float | None, str]:
        value = field_value(self.fields, *CONFIDENCE_FIELD_NAMES)
        return parse_score(value) if value else (None, "")

    def walk(self) -> Iterable["Message"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def descendants(self) -> list["Message"]:
        out: list[Message] = []
        for child in self.children:
            out.extend(child.walk())
        return out

    @property
    def followups(self) -> list["Message"]:
        """Everything said after this message, flattened and chronological."""
        return sorted(self.descendants(), key=lambda m: m.created)


@dataclass
class Paper:
    forum_id: str
    title: str
    venue: str
    venue_raw: str
    authors: list[str]
    authorids: list[str]
    keywords: list[str]
    abstract: str
    tldr: str
    primary_area: str
    forum_url: str
    pdf_url: str
    submission: Message | None
    reviews: list[Message] = field(default_factory=list)
    meta_reviews: list[Message] = field(default_factory=list)
    decisions: list[Message] = field(default_factory=list)
    orphan_comments: list[Message] = field(default_factory=list)
    withdrawn: bool = False
    note_count: int = 0
    fetched_at: float = 0.0
    source_query: str = ""
    resolved_via: str = ""
    warnings: list[str] = field(default_factory=list)

    # -- derived ---------------------------------------------------------- #
    @property
    def all_messages(self) -> list[Message]:
        if self.submission is None:
            return []
        return [self.submission, *self.submission.descendants()]

    @property
    def ratings(self) -> list[float]:
        out: list[float] = []
        for review in self.reviews:
            score, _ = review.rating()
            if score is not None:
                out.append(score)
        return out

    @property
    def mean_rating(self) -> float | None:
        ratings = self.ratings
        return sum(ratings) / len(ratings) if ratings else None

    @property
    def rating_spread(self) -> str:
        ratings = self.ratings
        if not ratings:
            return ""
        return f"min {min(ratings):g} / max {max(ratings):g}"

    @property
    def decision_text(self) -> str:
        for note in self.decisions:
            text = note.field("decision", "recommendation", "final_decision")
            if text:
                return text
        return ""

    @property
    def meta_review_text(self) -> str:
        for note in self.meta_reviews:
            text = note.field("metareview", "summary", "recommendation")
            if text:
                return text
        return ""

    def messages_of_kind(self, *kinds: str) -> list[Message]:
        return [m for m in self.all_messages if m.kind in kinds]

    @property
    def author_responses(self) -> list[Message]:
        return self.messages_of_kind("author_response")

    @property
    def comments(self) -> list[Message]:
        return self.messages_of_kind("comment")

    def reviewer_rows(self) -> list[dict[str, str]]:
        """One row per reviewer -- the at-a-glance comparison table."""
        rows: list[dict[str, str]] = []
        for review in self.reviews:
            rating, rating_meaning = review.rating()
            confidence, confidence_meaning = review.confidence()
            followups = review.descendants()
            replied = any(m.kind == "author_response" for m in followups)
            rows.append(
                {
                    "reviewer": review.label,
                    "id": review.anon_group or review.profile_id or review.signature,
                    "rating": (f"{rating:g}" if rating is not None
                               else field_value(review.fields, *RATING_FIELD_NAMES) or "—"),
                    "rating_meaning": rating_meaning,
                    "confidence": (f"{confidence:g}" if confidence is not None
                                   else field_value(review.fields,
                                                    *CONFIDENCE_FIELD_NAMES) or "—"),
                    "confidence_meaning": confidence_meaning,
                    "soundness": review.field("soundness") or "—",
                    "presentation": review.field("presentation") or "—",
                    "contribution": review.field("contribution") or "—",
                    "posted": review.created_str,
                    "followups": str(len(followups)),
                    "author_replied": "是" if replied else "否",
                    "questions": str(len(review_questions(review.fields))),
                    "url": review.url,
                }
            )
        return rows


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def to_datetime(millis: Any) -> datetime | None:
    try:
        value = int(millis)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 10_000_000_000:  # milliseconds
        value //= 1000
    try:
        return datetime.fromtimestamp(value)
    except (OSError, OverflowError, ValueError):
        return None


def split_questions(text: str) -> list[str]:
    """Split a free-text questions field into individual questions."""
    return [body for _, body in split_questions_numbered(text)]


# Ordinal / bullet markers reviewers actually use: "1.", "2)", "(3)", "Q4:",
# "-", "*", "•", and the markdown-bold variants "**Q1:**" / "**Answer 2:**"
# that authors use when they quote a question before answering it. The number
# is captured so answers can be paired to questions by their own numbering
# rather than by counting.
#
# The Q/A forms require a separator after the number. Without that, ordinary
# prose like "A 2019 study showed ..." would be mistaken for an answer marker.
# The separator class includes the full-width punctuation Chinese reviewers use
# ("1、", "2："). Without it a numbered list written that way was not recognised
# at all, so the raw "1、" stayed in the question text and the questions were
# never paired with their answers.
_ORDINAL_MARKER = re.compile(
    r"^\s*"
    r"(?:\*\*|__|\*|_)?\s*"                                  # optional emphasis
    r"(?:"
    r"\((?P<paren>\d+)\)"                                    # (1)
    r"|(?P<num>\d+)\s*[.):\]、。：．，）]"                     # 1.  2)  3、  4：
    r"|[Qq](?:uestion)?\s*(?P<qnum>\d+)\s*[:.)\]、。：）]"     # Q1:  Question 2.
    r"|[Aa](?:nswer)?\s*(?P<anum>\d+)\s*[:.)\]、。：）]"       # A1:  Answer 2.
    r"|问题\s*(?P<zhq>\d+)\s*[:.)\]、。：）]"                   # 问题1：
    r"|回答\s*(?P<zha>\d+)\s*[:.)\]、。：）]"                   # 回答1：
    r"|(?P<bullet>[-*\u2022\u00b7])"                         # bullet
    r")"
    r"\s*(?:\*\*|__)?\s*"                                    # closing emphasis
)


def marker_ordinal(match: re.Match[str]) -> int | None:
    """The question/answer number a marker carries, if any."""
    for group in ("paren", "num", "qnum", "anum", "zhq", "zha"):
        value = match.group(group)
        if value:
            return int(value)
    return None


def split_questions_numbered(text: str) -> list[tuple[int | None, str]]:
    """Split a field into ``(ordinal, text)`` blocks.

    The ordinal is the marker's number when the author used one (``1.``/``Q2:``),
    and ``None`` for bullets, which are ordered only by position.

    Two details matter for pairing:

    * free text before the first marker is a preamble ("We thank the reviewer
      ... answers below"), not an answer or a question. It is appended to the
      first block rather than becoming a block of its own, which would shift
      every subsequent pairing by one;
    * text with no markers at all is split on blank lines, and falls back to a
      single block rather than being mangled.
    """
    text = (text or "").strip()
    if not text:
        return []

    blocks: list[tuple[int | None, list[str]]] = []
    loose: list[str] = []
    for line in text.split("\n"):
        match = _ORDINAL_MARKER.match(line)
        if match:
            blocks.append((marker_ordinal(match),
                           [_ORDINAL_MARKER.sub("", line, count=1).strip()]))
        elif blocks:
            blocks[-1][1].append(line.strip())
        elif line.strip():
            loose.append(line.strip())

    if not blocks:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paragraphs) > 1:
            return [(None, p) for p in paragraphs]
        return [(None, text)]

    if loose:
        blocks[0][1].insert(0, "\n".join(loose))

    return [(number, "\n".join(body).strip()) for number, body in blocks
            if "".join(body).strip()]


# A lead-in line such as "My concerns are as follows:" / "Questions:" /
# "我的疑问如下：". The colon must end the line, otherwise ordinary prose like
# "the main question is: how do we scale?" would match.
_CONCERN_LEAD_IN = re.compile(
    r"^[^\n]{0,80}?(?:concerns?|questions?|issues?|comments?|疑问|问题)"
    r"[^\n]{0,40}?[:：]\s*$",
    re.IGNORECASE,
)

# A heading that is nothing but the name of a section: "**Weakness**",
# "## Concerns", "Weaknesses:". ICML 2026 writes reviews as
# "**Strength** <list> **Weakness** <list>", where the weakness list *is* the
# questions -- a reviewer there said so explicitly ("I have listed the
# questions in the weakness part").
_SECTION_LEAD_IN = re.compile(
    r"^\s*(?:\*\*|__|#{1,6}\s*)?\s*"
    r"(?:weakness(?:es)?|concerns?|questions?|issues?|"
    r"reasons?\s+to\s+reject|comments?|limitations?|"
    r"缺点|不足|疑问|问题|局限)\s*"
    r"(?:\*\*|__)?\s*[:：]?\s*$",
    re.IGNORECASE,
)


def _list_after(lines: list[str], start: int) -> list[str]:
    """Collect the list that begins after line ``start``, markers stripped."""
    items: list[list] = []
    index = start
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            if not items:
                index += 1
                continue
            # End the list unless another item follows the blank line.
            ahead = index + 1
            while ahead < len(lines) and not lines[ahead].strip():
                ahead += 1
            if ahead < len(lines) and _ORDINAL_MARKER.match(lines[ahead]):
                index = ahead
                continue
            break

        match = _ORDINAL_MARKER.match(lines[index])
        if match:
            body = _ORDINAL_MARKER.sub("", lines[index], count=1).strip()
            items.append([marker_ordinal(match), [body]])
        elif items:
            items[-1][1].append(stripped)
        else:
            break
        index += 1

    return ["\n".join(body).strip() for _, body in items
            if "".join(body).strip()]


def embedded_questions(text: str) -> list[str]:
    """Questions a reviewer wrote in the body instead of the questions field.

    This is not hypothetical. One NeurIPS 2025 reviewer put the literal string
    "NA" in the ``questions`` field and wrote "My concerns are as follows:"
    followed by a bulleted list inside ``strengths_and_weaknesses``; an ICML
    2026 reviewer wrote "I have listed the questions in the weakness part, so
    you can refer to it." and put them under a ``**Weakness**`` heading.

    Deliberately conservative: the prose lead-in ("... are as follows:") is
    preferred over a bare section heading, and either way the list stops at the
    first line that does not look like a continuing item.
    """
    lines = (text or "").split("\n")

    for pattern in (_CONCERN_LEAD_IN, _SECTION_LEAD_IN):
        for index, line in enumerate(lines):
            if not pattern.match(line.strip()):
                continue
            found = _list_after(lines, index + 1)
            if found:
                return found
    return []


# Where to look for buried questions, best first.
_QUESTION_FALLBACK_KEYS = (
    "strengths_and_weaknesses", "weaknesses", "reasons_to_reject",
    "summary", "final_justification", "review",
)


def review_questions(fields: dict[str, str]) -> list[tuple[int | None, str]]:
    """The reviewer's questions, from the questions field or from the body.

    Returns ``(ordinal, text)`` pairs, empty when the reviewer truly asked
    nothing -- which is the honest answer for a reviewer who wrote "NA" and
    then no list anywhere.

    Three situations are handled, all observed on real submissions:

    * the questions field holds the questions (ICLR, NeurIPS);
    * it holds a pointer instead ("I have listed the questions in the weakness
      part", ICML 2026) -- then the body is searched;
    * it is absent or a placeholder ("NA", NeurIPS 2025) -- likewise.
    """
    listed = field_value(fields, *QUESTION_FIELD_NAMES)
    if listed and not is_placeholder(listed) and not looks_like_pointer(listed):
        pairs = split_questions_numbered(listed)
        if pairs:
            return pairs

    for name in _QUESTION_FALLBACK_KEYS:
        key = field_key(fields, name)
        if not key:
            continue
        found = embedded_questions(fields[key])
        if found:
            return [(None, question) for question in found]
    return []


def extract_fields(note: dict, kind: str) -> dict[str, str]:
    """Pull the meaningful content fields out of a note, cleaned.

    Checklist acknowledgements and empty-sounding placeholders ("NA") are
    dropped here rather than at render time, so every consumer -- reports, the
    Q&A pairing, the copied plain text -- sees the same filtered view.
    """
    content = unwrap_content(note.get("content") or {})
    fields: dict[str, str] = {}
    for key, value in content.items():
        if key in _NOISE_KEYS or key.startswith("_"):
            continue
        if key.lower() in _ACKNOWLEDGEMENT_KEYS:
            continue
        text = clean_text(value)
        if not text or is_placeholder(text):
            continue
        fields[key] = text
    return fields


def order_fields(fields: dict[str, str], kind: str) -> list[tuple[str, str, str]]:
    """Return ``(key, label, value)`` in display order, unknowns last.

    Matching is case-insensitive because venues disagree about casing:
    EMNLP/ACL reviews store ``Questions_for_the_Authors`` where ICLR stores
    ``questions``. Without that, every EMNLP field fell through to the
    "unknown" tail and was labelled with a humanized key name.
    """
    if kind == "review":
        preferred = REVIEW_FIELDS
    elif kind == "meta_review":
        preferred = META_FIELDS
    elif kind == "decision":
        preferred = DECISION_FIELDS
    else:
        preferred = []

    out: list[tuple[str, str, str]] = []
    used: set[str] = set()
    lowered = {key.lower(): key for key in fields}

    for key, label in preferred:
        actual = lowered.get(key.lower())
        if actual is not None and actual not in used:
            out.append((actual, label, fields[actual]))
            used.add(actual)

    if kind == "review":
        for name in RATING_FIELD_NAMES + CONFIDENCE_FIELD_NAMES:
            actual = lowered.get(name.lower())
            if actual is not None and actual not in used:
                title = "评分 Rating" if name in RATING_FIELD_NAMES else "置信度 Confidence"
                out.insert(0, (actual, title, fields[actual]))
                used.add(actual)

    for key, value in fields.items():
        if key in used:
            continue
        out.append((key, FIELD_LABELS.get(key, humanize(key)), value))
    return out


def anon_group_id(signature: str) -> str:
    """The anonymous group name, e.g. ``Reviewer_abc``.

    This is what OpenReview itself shows for a masked reviewer, and unlike the
    bare suffix (``abc``) it is unambiguous about the role.
    """
    segment = (signature or "").rstrip("/").rsplit("/", 1)[-1]
    if not segment or segment.startswith("~"):
        return ""
    for prefix, _ in _ROLE_PREFIXES:
        if segment == prefix or segment.startswith(prefix + "_"):
            return segment
    return ""


def _make_message(note: dict, kind: str, forum_id: str) -> Message:
    signature = primary_signature(note)
    role, anon_id, profile_id = parse_signature(signature)
    created = note.get("cdate") or note.get("tcdate") or note.get("mdate") or 0
    modified = note.get("mdate") or created
    return Message(
        id=str(note.get("id") or ""),
        kind=kind,
        role=role,
        signature=signature,
        anon_id=anon_id,
        anon_group=anon_group_id(signature),
        profile_id=profile_id,
        created=int(created or 0),
        modified=int(modified or 0),
        fields=extract_fields(note, kind),
        url=f"https://openreview.net/forum?id={forum_id}&noteId={note.get('id')}",
        raw=note,
    )


def _label_message(message: Message, index: int | None = None) -> None:
    """Give a message a short human label such as ``Reviewer #2``."""
    if message.kind == "review" and message.role == "Reviewer":
        if index is not None:
            message.label = f"Reviewer #{index}"
        elif message.anon_id:
            message.label = f"Reviewer {message.anon_id}"
        else:
            message.label = "Reviewer"
    elif message.kind == "review":
        message.label = message.role or "Reviewer"
    elif message.role == "Authors" or message.kind == "author_response":
        message.label = "Authors（作者）"
    elif message.role:
        message.label = message.role
    else:
        message.label = "Anonymous"


VENUE_SHORT = re.compile(r"^([A-Za-z][A-Za-z0-9\-]*(?:\s+[A-Za-z0-9\-]+)?)\s+(\d{4})")

# "Submitted to ICLR 2024", "Accepted at NeurIPS 2023", ... -- the status
# prefix carries no information the report does not already show separately.
VENUE_STATUS_PREFIX = re.compile(
    r"^\s*(?:submitted to|resubmitted to|accepted (?:at|by|to)|published (?:at|in)|"
    r"under review (?:at|for)|to appear (?:at|in))\s+",
    re.IGNORECASE,
)


def short_venue(venue: str) -> str:
    """``"Submitted to ICLR 2024"`` -> ``"ICLR 2024"``.

    Workshop and track names are kept whole, because collapsing
    "NeurIPS 2023 Workshop on X" to "NeurIPS 2023" would merge genuinely
    different venues into one label.
    """
    text = (venue or "").strip()
    while True:
        stripped = VENUE_STATUS_PREFIX.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    if not text:
        return ""
    lowered = text.lower()
    if any(word in lowered for word in ("workshop", "track", "findings", "tutorial")):
        return text
    match = VENUE_SHORT.match(text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return text


def venue_from_id(venueid: str) -> str:
    """``"ICLR.cc/2024/Conference"`` -> ``"ICLR 2024"``."""
    parts = [p for p in (venueid or "").split("/") if p and not p.startswith("-")]
    if not parts:
        return ""
    name = parts[0].split(".")[0]
    year = next((p for p in parts[1:] if re.fullmatch(r"\d{4}", p)), "")
    return f"{name} {year}".strip()


def display_venue(venue: str, venueid: str) -> str:
    """Prefer the human venue string; fall back to the venue id path."""
    short = short_venue(venue)
    if short:
        return short
    return venue_from_id(venueid)


# --------------------------------------------------------------------------- #
# main entry point
# --------------------------------------------------------------------------- #
def build_paper(
    notes: list[dict],
    forum_id: str,
    submission_note: dict | None = None,
    source_query: str = "",
    resolved_via: str = "",
) -> Paper:
    """Rebuild a whole forum thread into a :class:`Paper`."""
    warnings: list[str] = []
    if not notes:
        raise ValueError(f"no notes for forum {forum_id}")

    by_id: dict[str, dict] = {str(n.get("id")): n for n in notes}
    root_note = submission_note or by_id.get(forum_id)
    if root_note is None:
        root_note = next(
            (n for n in notes if not n.get("replyto")), notes[0]
        )
        warnings.append(
            f"submission note {forum_id} was not in the thread; used {root_note.get('id')}"
        )
    forum_id = str(root_note.get("id") or forum_id)

    content = unwrap_content(root_note.get("content") or {})
    authors = content.get("authors") or []
    authorids = content.get("authorids") or []
    keywords = content.get("keywords") or []
    if isinstance(authors, str):
        authors = [authors]
    if isinstance(authorids, str):
        authorids = [authorids]
    if isinstance(keywords, str):
        keywords = [keywords]

    submission = _make_message(root_note, "submission", forum_id)
    submission.label = "Submission（论文）"

    # -- classify + link -------------------------------------------------- #
    messages: dict[str, Message] = {}
    parents: dict[str, str] = {}
    for note in notes:
        note_id = str(note.get("id"))
        if note_id == forum_id:
            continue
        kind = classify(note, forum_id)
        message = _make_message(note, kind, forum_id)
        messages[note_id] = message
        replyto = note.get("replyto")
        parents[note_id] = str(replyto) if replyto else forum_id
        venue = message.fields.get("venue") or ""
        if kind == "withdrawal" or "withdraw" in venue.lower():
            pass  # handled below via the submission's venue

    # Attachment: notes whose parent is missing get hung off the submission so
    # nothing is ever silently dropped from the report.
    for note_id, message in messages.items():
        parent_id = parents.get(note_id) or forum_id
        if parent_id == forum_id or parent_id not in messages:
            if parent_id not in messages and parent_id != forum_id:
                warnings.append(
                    f"note {note_id} referenced missing parent {parent_id}; "
                    "attached to the submission"
                )
            submission.children.append(message)
        else:
            messages[parent_id].children.append(message)

    for message in [submission, *messages.values()]:
        message.children.sort(key=lambda m: (m.created, m.id))

    def assign_depth(node: Message, depth: int) -> None:
        node.depth = depth
        for child in node.children:
            assign_depth(child, depth + 1)

    assign_depth(submission, 0)

    # -- reviewer numbering ----------------------------------------------- #
    reviews: list[Message] = []
    meta_reviews: list[Message] = []
    decisions: list[Message] = []
    for message in messages.values():
        if message.kind == "review":
            reviews.append(message)
        elif message.kind == "meta_review":
            meta_reviews.append(message)
        elif message.kind == "decision":
            decisions.append(message)
    reviews.sort(key=lambda m: (m.created, m.id))
    for i, review in enumerate(reviews, start=1):
        review.label = f"Reviewer #{i}"
        identity = review.anon_group or review.profile_id
        if identity:
            review.label += f" ({identity})"

    for message in messages.values():
        if message.kind != "review":
            _label_message(message)

    # Comments posted straight onto the paper (not under any review).
    orphan_comments = sorted(
        (c for c in submission.children if c.kind in ("comment", "author_response")),
        key=lambda m: m.created,
    )

    venue_raw = str(content.get("venue") or "")
    venueid = str(content.get("venueid") or "")
    withdrawn = "withdraw" in venue_raw.lower() or "withdraw" in venueid.lower()

    if not reviews and not meta_reviews:
        warnings.append(
            "no reviews found. They may not be public yet, or the venue hides "
            "reviews until the discussion period; retry with --token if you have access."
        )

    return Paper(
        forum_id=forum_id,
        title=str(content.get("title") or "").strip() or f"(untitled {forum_id})",
        venue=display_venue(venue_raw, venueid),
        venue_raw=venue_raw or venueid,
        authors=[clean_text(a) for a in authors if a],
        authorids=[str(a) for a in authorids if a],
        keywords=[clean_text(k) for k in keywords if k],
        abstract=clean_text(content.get("abstract")),
        tldr=clean_text(content.get("TLDR") or content.get("tldr")),
        primary_area=clean_text(content.get("primary_area")),
        forum_url=f"https://openreview.net/forum?id={forum_id}",
        pdf_url=f"https://openreview.net/pdf?id={forum_id}",
        submission=submission,
        reviews=reviews,
        meta_reviews=sorted(meta_reviews, key=lambda m: m.created),
        decisions=sorted(decisions, key=lambda m: m.created),
        orphan_comments=orphan_comments,
        withdrawn=withdrawn,
        note_count=len(notes),
        source_query=source_query,
        resolved_via=resolved_via,
        warnings=warnings,
    )
