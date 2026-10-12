"""Pair a reviewer's questions with the authors' answers.

This is the part that makes the report actually useful to read: a reviewer
writes five numbered questions, the authors answer them, and in the raw API
that is two undifferentiated blobs of text separated by a week of thread
traffic.

The pairing is heuristic and says so. Reviewers and authors usually -- but not
always -- use matching numbering, so:

* if the answers split into exactly as many blocks as there are questions, pair
  them positionally;
* if there are fewer, pair what we can and leave the rest unanswered;
* if there are more, hand the leftovers to the last question rather than
  dropping them.

When the numbering does not line up at all we deliberately return the answers
as a single attached block instead of guessing, because a wrong pairing is far
more misleading than an unpaired one.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .extract import Message, review_questions, split_questions_numbered

_ANSWER_KEYS = ("rebuttal", "response", "comment", "author_response", "reply", "answer")

# Three spellings of the same idea appear in the wild:
#
#   "**Q2:** ... **A2:** ..."            quoted question and answer
#   "2. <point>  3. <point>"             numbered points
#   "**Comment 2**. <point> **Response**: <answer>"   the NeurIPS 2025 style
#
# The last one is the most trustworthy of the three, because the author names
# the exact point they are answering instead of relying on matching numbers.
_FULLWIDTH = r":.)\]、。：）"
_Q_MARKER = re.compile(
    rf"(?m)^[ \t]*(?:\*\*|__|\*|_)?[ \t]*[Qq](?:uestion)?[ \t]*(\d+)[ \t]*[{_FULLWIDTH}]"
)
_A_MARKER = re.compile(
    rf"(?m)^[ \t]*(?:\*\*|__|\*|_)?[ \t]*[Aa](?:nswer)?[ \t]*(\d+)[ \t]*[{_FULLWIDTH}]"
)
_Q_LINE = re.compile(
    rf"^[ \t]*(?:\*\*|__|\*|_)?[ \t]*[Qq](?:uestion)?[ \t]*(\d+)[ \t]*[{_FULLWIDTH}][ \t]*"
)
_A_LINE = re.compile(
    rf"^[ \t]*(?:\*\*|__|\*|_)?[ \t]*[Aa](?:nswer)?[ \t]*(\d+)[ \t]*[{_FULLWIDTH}][ \t]*"
)
_COMMENT_LINE = re.compile(
    r"^[ \t]*(?:\*\*|__)?[ \t]*(?:comment|point|concern|weakness|issue|item)"
    r"[ \t]*(\d+)?[ \t]*(?:\*\*|__)?[ \t]*[.:：、)）][ \t]*(?:\*\*|__)?[ \t]*",
    re.IGNORECASE,
)
_RESPONSE_LINE = re.compile(
    r"^[ \t]*(?:\*\*|__)?[ \t]*(?:response|reply|answer|rebuttal|回复|回答)"
    r"[ \t]*(\d+)?[ \t]*(?:\*\*|__)?[ \t]*[.:：、)）]?[ \t]*(?:\*\*|__)?[ \t]*",
    re.IGNORECASE,
)


@dataclass
class AuthorBlock:
    """One point in an author's response.

    ``quoted_question`` is the author's own restatement of the point they are
    answering -- the anchor that lines their answers up with the reviewer's
    questions, and the only reliable one when the two numbering schemes differ.
    ``label`` names the block the way the author did ("Comment 2"), so a
    matched answer can be traced back to the response text.
    """

    ordinal: int | None = None
    quoted_question: str = ""
    answer: str = ""
    has_answer: bool = False
    label: str = ""


def parse_author_blocks(text: str) -> list[AuthorBlock]:
    """Parse a structured author response into blocks.

    Handles the Q/A convention and the Comment/Response convention, including
    the case where a Response marker has no matching Comment marker (the author
    answers in bulk).
    """
    blocks: list[AuthorBlock] = []
    current: AuthorBlock | None = None

    for line in text.split("\n"):
        q_match = _Q_LINE.match(line)
        a_match = None if q_match else _A_LINE.match(line)
        c_match = None if (q_match or a_match) else _COMMENT_LINE.match(line)
        r_match = None if (q_match or a_match or c_match) else _RESPONSE_LINE.match(line)

        if q_match or c_match:
            pattern = _Q_LINE if q_match else _COMMENT_LINE
            match = q_match or c_match
            number = match.group(1)
            current = AuthorBlock(
                ordinal=int(number) if number else None,
                quoted_question=pattern.sub("", line, count=1).strip(),
                label=("Comment " + number) if (c_match and number) else "",
            )
            blocks.append(current)
        elif a_match or r_match:
            pattern = _A_LINE if a_match else _RESPONSE_LINE
            match = a_match or r_match
            number = match.group(1)
            # An answer marker after a quoted point belongs to that point;
            # otherwise it starts a block of its own.
            if current is None or current.has_answer:
                current = AuthorBlock(ordinal=int(number) if number else None)
                blocks.append(current)
            current.answer = pattern.sub("", line, count=1).strip()
            current.has_answer = True
        elif current is not None:
            if current.has_answer:
                current.answer = f"{current.answer}\n{line.strip()}".strip()
            else:
                current.quoted_question = (
                    f"{current.quoted_question}\n{line.strip()}".strip()
                )
    for block in blocks:
        block.answer = block.answer.strip().strip("*_").strip()
        block.quoted_question = block.quoted_question.strip().strip("*_").strip()
    return [b for b in blocks if b.answer or b.quoted_question]


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _text_similarity(left: str, right: str) -> float:
    """Token-overlap blended with sequence similarity, 0..1."""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    jaccard = len(a & b) / len(a | b)
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return max(jaccard, ratio, 0.5 * jaccard + 0.5 * ratio)


def answers_by_ordinal(text: str) -> dict[int, str]:
    """Map question number -> answer text for a quoted Q/A style response.

    Falls back to splitting on ``Qk:`` markers when the author did not label
    their answers with ``Ak:``. Returns an empty dict when the response is not
    written in that style at all, so the caller can use positional pairing.
    """
    for marker in (_A_MARKER, _Q_MARKER):
        hits = [(m.start(), m.end(), int(m.group(1))) for m in marker.finditer(text)]
        if not hits:
            continue
        # An answer runs until whichever marker comes next -- a later answer, a
        # quoted question, or the end of the text.
        boundaries = sorted(
            [h[0] for h in hits]
            + [m.start() for m in _Q_MARKER.finditer(text)]
            + [m.start() for m in _A_MARKER.finditer(text)]
            + [len(text)]
        )
        out: dict[int, str] = {}
        for start, end, number in hits:
            following = next((b for b in boundaries if b > start), len(text))
            body = text[end:following].strip()
            body = body.strip("*_").strip()
            if body and number not in out:
                out[number] = body
        if out:
            return out
    return {}


@dataclass
class QAItem:
    question: str
    answer: str = ""
    answer_label: str = ""
    answer_time: str = ""
    answer_url: str = ""
    answer_block: str = ""
    matched: bool = False
    followups: list[Message] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        return bool(self.answer.strip())


@dataclass
class QASet:
    questions: list[QAItem] = field(default_factory=list)
    unmatched_answers: list[Message] = field(default_factory=list)
    note: str = ""


def _answer_text(message: Message) -> str:
    for key in _ANSWER_KEYS:
        text = message.field(key)
        if text:
            return text
    # Fall back to the longest field -- usually the whole response body.
    if message.fields:
        return max(message.fields.values(), key=len)
    return ""


def _pick_primary_response(responses: list[Message]) -> Message:
    """The response most likely to hold the answers, not merely the first.

    Authors often post a short "we are preparing a response" note before the
    real rebuttal, so the earliest response is frequently the least useful one.
    """
    if len(responses) == 1:
        return responses[0]
    return max(responses, key=lambda m: len(_answer_text(m)))


def build_qa(review: Message, responses: list[Message] | None = None) -> QASet:
    """Build the question/answer view for one review.

    This is the part that has to be right, and it is genuinely hard because
    *the author's numbering is not the reviewer's numbering*. One real response
    answered points the authors labelled Q2..Q5 while the reviewer had asked
    Q1..Q3, so pairing the reviewer's Q3 with the author's A3 produced a
    confident, plausible, wrong answer.

    So the strategy is content-first:

    1. Authors usually restate each point before answering it ("**Q3:** Are any
       significant tests performed?", "**Comment 3**. The ablation is
       missing."). Match the reviewer's questions against those restatements by
       text similarity and pair the best one-to-one matches. This is what
       survives a numbering mismatch.
    2. If the response restates nothing, fall back to pairing by number.
    3. If numbering is absent too, pair positionally when the counts line up.
    4. Otherwise attach the whole response to the first question, marked
       unmatched -- a wrong pairing is far worse than an unpaired one.

    When the reviewer asked nothing answerable (a field containing only "NA"),
    there is no pairing to make; the caller shows the response thread instead of
    a fabricated Q/A list.
    """
    question_pairs = review_questions(review.fields)
    if not question_pairs:
        return QASet()

    if responses is None:
        responses = [m for m in review.followups if m.kind == "author_response"]
    responses = [m for m in responses if m.kind == "author_response"]

    questions = [body for _, body in question_pairs]
    items = [QAItem(question=q) for q in questions]
    if not responses:
        return QASet(
            questions=items,
            note="作者尚未公开回复（或回复被 venue 隐藏）。",
        )

    primary = _pick_primary_response(responses)
    remainder = [m for m in responses if m is not primary]
    answer_text = _answer_text(primary)

    blocks = [b for b in parse_author_blocks(answer_text)
              if b.quoted_question and b.answer]
    matched_by_content = _match_by_quoted_text(items, question_pairs, blocks)

    if not matched_by_content:
        answer_pairs = split_questions_numbered(answer_text)
        # Prefer explicit "A1:"/"Q1:" labels; fall back to positional numbering.
        by_number = answers_by_ordinal(answer_text)
        if not by_number:
            by_number = {n: body for n, body in answer_pairs if n is not None}
        questions_numbered = all(n is not None for n, _ in question_pairs)

        if questions_numbered and by_number:
            for index, (number, _) in enumerate(question_pairs):
                if number in by_number:
                    items[index].answer = by_number[number]
                    items[index].matched = True
        elif len(answer_pairs) == len(items) and len(items) > 1:
            for item, (_, body) in zip(items, answer_pairs):
                item.answer = body
                item.matched = True
        elif 1 < len(answer_pairs) < len(items):
            for item, (_, body) in zip(items, answer_pairs):
                item.answer = body
                item.matched = True
        else:
            items[0].answer = answer_text

    # Every answer comes from the same response note, so they all get the same
    # note attribution; the block label (when the author named their points)
    # says which part of it answered this question.
    for item in items:
        if item.answer:
            item.answer_label = primary.label
            item.answer_time = primary.created_str
            item.answer_url = primary.url
    if remainder:
        items[-1].followups.extend(remainder)

    filled = sum(1 for item in items if item.answered)
    note = ""
    if filled < len(items):
        note = (
            f"{len(items)} 个问题中有 {filled} 个在作者回复里找到了明确对应的一段；"
            "其余请见下方完整讨论串。"
        )
    return QASet(questions=items, unmatched_answers=remainder, note=note)


def _match_by_quoted_text(items: list[QAItem], question_pairs: list[tuple[int | None, str]],
                          blocks: list[AuthorBlock],
                          threshold: float = 0.42) -> bool:
    """Pair reviewer questions with the author's restated points by similarity.

    Returns True when at least one pair was made, in which case the caller must
    not fall back to number-based pairing: mixing the two schemes is exactly how
    a plausible wrong answer gets produced.
    """
    if not blocks:
        return False

    scored: list[tuple[float, int, int]] = []
    for question_index, (_, question_text) in enumerate(question_pairs):
        for block_index, block in enumerate(blocks):
            scored.append(
                (_text_similarity(question_text, block.quoted_question),
                 question_index, block_index)
            )
    scored.sort(key=lambda row: row[0], reverse=True)

    used_questions: set[int] = set()
    used_blocks: set[int] = set()
    matched = False
    for score, question_index, block_index in scored:
        if score < threshold:
            break
        if question_index in used_questions or block_index in used_blocks:
            continue
        used_questions.add(question_index)
        used_blocks.add(block_index)
        block = blocks[block_index]
        items[question_index].answer = block.answer
        items[question_index].matched = True
        # Name the author's block so the answer can be traced in the thread.
        items[question_index].answer_block = block.label
        matched = True
    return matched
