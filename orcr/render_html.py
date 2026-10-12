"""Single-file HTML renderer.

Everything -- CSS, JS, data -- is inlined so the report opens by double-click
with no server, no CDN and no network. That constraint is deliberate: the
report has to keep working years after the OpenReview page changes, and it has
to be emailable to a collaborator as one file.

What the page gives you:

* one tab per paper when a batch was crawled;
* a sticky table of contents;
* full-text search with match highlighting, scoped to the visible paper;
* filter chips for reviews / author responses / discussion;
* per-paper reviewer comparison table with relatively-coloured score badges,
  so the harshest and friendliest reviewer are obvious at a glance;
* each review as a collapsible card holding the paired Q&A followed by the
  complete, nested discussion thread.
"""

from __future__ import annotations

import time
from datetime import datetime
from html import escape

from .extract import (
    NON_CONTENT_KINDS,
    Message,
    Paper,
    order_fields,
    parse_score,
)
from .markdown import looks_like_markdown, render_markdown
from .qa import build_qa


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _e(value: object) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _fmt_dt(millis: int) -> str:
    if not millis:
        return "时间未知"
    try:
        return datetime.fromtimestamp(millis / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "时间未知"


def _score_color(value: float | None, low: float | None, high: float | None) -> str:
    """Relative colour: red for the harshest reviewer on this paper, green for
    the most generous. Relative rather than absolute because the rating scale
    differs per venue (1-10 at ICLR, 1-6 at older NeurIPS, 1-5 at ICML)."""
    if value is None:
        return "background:#eceff3;color:#5b6673"
    if low is None or high is None or high <= low:
        fraction = 0.5
    else:
        fraction = (value - low) / (high - low)
    fraction = max(0.0, min(1.0, fraction))
    hue = 8 + fraction * 128  # 8 = red, 136 = green
    return f"background:hsl({hue:.0f} 72% 90%);color:hsl({hue:.0f} 62% 26%);border-color:hsl({hue:.0f} 55% 74%)"


def _chip(text: str, kind: str = "") -> str:
    if not text or text == "—":
        return '<span class="chip chip-empty">—</span>'
    return f'<span class="chip{" " + kind if kind else ""}">{_e(text)}</span>'


def _body_html(text: str) -> str:
    """Render a field, keeping both the rendered and the original text.

    The rendered copy is what the reader wants; the original is kept alongside
    so the toolbar's 原文 toggle can always show exactly what OpenReview
    stores. That matters because the Markdown renderer is a deliberate subset --
    when it guesses wrong, the source is one click away rather than lost.
    """
    text = text or ""
    if not text.strip():
        return '<div class="body muted">（无正文）</div>'
    rendered = render_markdown(text)
    original = f'<pre class="body raw-body" hidden>{_e(text)}</pre>'
    return f'<div class="md-body">{rendered}</div>{original}'


# --------------------------------------------------------------------------- #
# fragments
# --------------------------------------------------------------------------- #
def _message_html(message: Message, depth: int = 0) -> str:
    kind_class = {
        "author_response": "msg-author",
        "comment": "msg-comment",
        "review": "msg-review",
        "meta_review": "msg-meta",
        "decision": "msg-decision",
    }.get(message.kind, "msg-comment")

    body = message.field("rebuttal", "response", "comment")
    if not body and message.fields:
        body = max(message.fields.values(), key=len)
    extra = ""
    if message.kind not in ("author_response", "comment") or len(message.fields) > 1:
        rows = [
            f'<div class="kv"><span class="k">{_e(label)}</span>{_body_html(value)}</div>'
            for key, label, value in order_fields(message.fields, message.kind)
            if value != body
        ]
        if rows:
            extra = '<div class="kv-list">' + "".join(rows) + "</div>"

    children = "".join(_message_html(child, depth + 1) for child in message.children
                       if child.kind not in NON_CONTENT_KINDS)
    style = f' style="--depth:{min(depth, 6)}"' if depth else ""
    return (
        f'<div class="msg {kind_class}" data-kind="{_e(message.kind)}"{style}>'
        f'<div class="msg-head">'
        f'<span class="who">{_e(message.label or message.role or "Anonymous")}</span>'
        f'<span class="role">{_e(message.role_zh)}</span>'
        f'<time>{_e(message.created_str)}</time>'
        f'<a class="src" href="{_e(message.url)}" target="_blank" rel="noopener">原文 ↗</a>'
        f"</div>"
        f"{_body_html(body)}"
        f"{extra}"
        f"{children}"
        f"</div>"
    )


def _qa_html(review: Message) -> str:
    qa = build_qa(review)
    if not qa.questions:
        return ""
    rows: list[str] = []
    for index, item in enumerate(qa.questions, start=1):
        if item.answered:
            answer = (
                f'<div class="answer">'
                f'<div class="answer-head">A{index} · {_e(item.answer_label)} · '
                f'<time>{_e(item.answer_time)}</time>'
                + (f' · 对应作者 {_e(item.answer_block)}' if item.answer_block else "")
                + f'{" · <a href=" + chr(34) + _e(item.answer_url) + chr(34) + " target=_blank rel=noopener>原文 ↗</a>" if item.answer_url else ""}'
                f"</div>"
                f"{_body_html(item.answer)}"
                f"</div>"
            )
            badge = ""
        else:
            answer = '<div class="answer answer-missing">未找到明确对应的回复，请见下方完整讨论串。</div>'
            badge = ""
        rows.append(
            f'<div class="qa">'
            f'<div class="question"><span class="qtag">Q{index}</span>{_body_html(item.question)}</div>'
            f"{answer}{badge}</div>"
        )
    note = f'<p class="note">{_e(qa.note)}</p>' if qa.note else ""
    return f'<section class="qa-set"><h4>审稿问题 ↔ 作者回复</h4>{note}{"".join(rows)}</section>'


def _review_card(review: Message, paper: Paper, low: float | None,
                 high: float | None, index: int) -> str:
    rating, meaning = parse_score(review.field("rating", "overall_assessment"))
    confidence, confidence_meaning = parse_score(review.field("confidence"))
    colour = _score_color(rating, low, high)

    fields_html: list[str] = []
    for key, label, value in order_fields(review.fields, "review"):
        if key == "questions":
            continue
        fields_html.append(
            f'<div class="field"><h5>{_e(label)}</h5>{_body_html(value)}</div>'
        )

    followups = [c for c in review.children]
    thread = (
        '<section class="thread"><h4>讨论串</h4>'
        + "".join(_message_html(node, 0) for node in followups)
        + "</section>"
        if followups
        else '<section class="thread"><h4>讨论串</h4><p class="note">'
             "该审稿意见下暂无后续讨论。</p></section>"
    )

    replied = any(m.kind == "author_response" for m in review.descendants())
    return (
        f'<details class="card review-card" open data-kind="review" '
        f'data-search="{_e(review.label + " " + (review.anon_id or "") + " " + review.signature)}">'
        f'<summary>'
        f'<span class="rlabel">{_e(review.label)}</span>'
        f'<span class="badge" style="{colour}">评分 {_e(f"{rating:g}" if rating is not None else "—")}</span>'
        f'<span class="badge badge-plain">置信度 {_e(f"{confidence:g}" if confidence is not None else "—")}</span>'
        f'<span class="badge badge-plain">{len(review.descendants())} 条后续</span>'
        f'<span class="badge {"badge-ok" if replied else "badge-warn"}">'
        f'{"作者已回复" if replied else "作者未回复"}</span>'
        f'<span class="spacer"></span>'
        f'<time>{_e(review.created_str)}</time>'
        f'<button class="copy" type="button" data-copy="review-{index}">复制</button>'
        f"</summary>"
        f'<div class="card-meta">'
        f'<span>匿名 ID <code>{_e(review.anon_id or "—")}</code></span>'
        + (f'<span>公开身份 <code>{_e(review.profile_id)}</code></span>' if review.profile_id else "")
        + f'<span>{_e(review.role_zh)}</span>'
        f'<a href="{_e(review.url)}" target="_blank" rel="noopener">在 OpenReview 上查看 ↗</a>'
        f"</div>"
        + (f'<p class="rating-meaning">{_e(meaning)}</p>' if meaning else "")
        + (f'<p class="rating-meaning">置信度：{_e(confidence_meaning)}</p>' if confidence_meaning else "")
        + _qa_html(review)
        + '<section class="full"><h4>审稿全文</h4>' + "".join(fields_html) + "</section>"
        + thread
        + f'<div class="copy-source" id="review-{index}" hidden>{_e(_review_plaintext(review))}</div>'
        + "</details>"
    )


def _review_plaintext(review: Message) -> str:
    parts = [review.label, f"评分: {review.field('rating', 'overall_assessment')}"]
    for key, label, value in order_fields(review.fields, "review"):
        parts.append(f"\n## {label}\n{value}")
    for node in review.descendants():
        body = node.field("rebuttal", "response", "comment") or ""
        parts.append(f"\n--- {node.label} ({node.created_str}) ---\n{body}")
    return "\n".join(parts).strip()


def _summary_table(paper: Paper) -> str:
    rows = paper.reviewer_rows()
    if not rows:
        return '<p class="note">未抓取到审稿意见。</p>'
    ratings = [r for r in paper.ratings]
    low = min(ratings) if ratings else None
    high = max(ratings) if ratings else None
    out = [
        '<table class="grid"><thead><tr>',
        "<th>审稿人</th><th>匿名 ID</th><th>评分</th><th>置信度</th>",
        "<th>Soundness</th><th>Presentation</th><th>Contribution</th>",
        "<th>问题数</th><th>后续发言</th><th>作者已回复</th><th>提交时间</th>",
        "</tr></thead><tbody>",
    ]
    for row in rows:
        value = None
        try:
            value = float(row["rating"])
        except (TypeError, ValueError):
            value = None
        colour = _score_color(value, low, high)
        anchor = _anchor(row["reviewer"])
        out.append(
            f'<tr><td><a href="#{anchor}">{_e(row["reviewer"])}</a></td>'
            f'<td><code>{_e(row["id"])}</code></td>'
            f'<td><span class="badge" style="{colour}">{_e(row["rating"])}</span>'
            + (f'<div class="tiny">{_e(row["rating_meaning"])}</div>' if row["rating_meaning"] else "")
            + "</td>"
            f'<td>{_e(row["confidence"])}'
            + (f'<div class="tiny">{_e(row["confidence_meaning"])}</div>' if row["confidence_meaning"] else "")
            + "</td>"
            f'<td>{_e(row["soundness"])}</td><td>{_e(row["presentation"])}</td>'
            f'<td>{_e(row["contribution"])}</td><td>{_e(row["questions"])}</td>'
            f'<td>{_e(row["followups"])}</td><td>{_e(row["author_replied"])}</td>'
            f'<td class="tiny">{_e(row["posted"])}</td></tr>'
        )
    out.append("</tbody></table>")
    return "".join(out)


def _anchor(label: str) -> str:
    out = []
    for char in label.lower():
        if char.isalnum():
            out.append(char)
        elif char in " #(":
            out.append("-")
    return "r-" + "".join(out).strip("-").replace("--", "-")


def _paper_section(paper: Paper, index: int, active: bool) -> str:
    ratings = paper.ratings
    low = min(ratings) if ratings else None
    high = max(ratings) if ratings else None

    meta_rows = [
        ("Venue", _e(paper.venue or "—")),
        ("OpenReview", f'<a href="{_e(paper.forum_url)}" target="_blank" rel="noopener">{_e(paper.forum_id)} ↗</a>'),
        ("PDF", f'<a href="{_e(paper.pdf_url)}" target="_blank" rel="noopener">下载 PDF ↗</a>'),
    ]
    if paper.authors:
        meta_rows.append(("作者", _e("、".join(paper.authors))))
    if paper.primary_area:
        meta_rows.append(("领域", _e(paper.primary_area)))
    if paper.keywords:
        meta_rows.append(("关键词", _e("、".join(paper.keywords))))
    meta_rows.append(("审稿人数", f"{len(paper.reviews)}"))
    if paper.mean_rating is not None:
        meta_rows.append(
            ("平均评分", f'<strong>{paper.mean_rating:.2f}</strong> '
                         f'<span class="tiny">({_e(paper.rating_spread)})</span>')
        )
    if paper.decision_text:
        meta_rows.append(("最终决定", f'<strong class="decision">{_e(paper.decision_text)}</strong>'))

    meta_html = "".join(
        f'<div class="meta-item"><span class="mk">{k}</span><span class="mv">{v}</span></div>'
        for k, v in meta_rows
    )

    warnings = "".join(
        f'<div class="warn">⚠️ {_e(w)}</div>' for w in paper.warnings
    )

    abstract = (
        f'<details class="card"><summary><span class="rlabel">摘要 Abstract</span></summary>'
        f'{_body_html(paper.abstract)}</details>'
        if paper.abstract else ""
    )

    meta_review = ""
    for meta in paper.meta_reviews:
        body = "".join(
            f'<div class="field"><h5>{_e(label)}</h5>{_body_html(value)}</div>'
            for _, label, value in order_fields(meta.fields, "meta_review")
        )
        meta_review += (
            f'<details class="card" open data-kind="meta_review">'
            f'<summary><span class="rlabel">🧭 元审稿意见 Meta Review</span>'
            f'<span class="spacer"></span><time>{_e(meta.created_str)}</time></summary>'
            f'<div class="card-meta"><a href="{_e(meta.url)}" target="_blank" rel="noopener">原文 ↗</a></div>'
            f"{body}</details>"
        )

    decision = ""
    for note in paper.decisions:
        body = "".join(
            f'<div class="field"><h5>{_e(label)}</h5>{_body_html(value)}</div>'
            for _, label, value in order_fields(note.fields, "decision")
        )
        decision += (
            f'<details class="card decision-card" open data-kind="decision">'
            f'<summary><span class="rlabel">⚖️ 最终决定 Decision</span>'
            f'<span class="spacer"></span><time>{_e(note.created_str)}</time></summary>'
            f"{body}</details>"
        )

    cards = "".join(
        _review_card(review, paper, low, high, i)
        for i, review in enumerate(paper.reviews, start=1)
    )
    if not cards:
        cards = ('<p class="note">没有找到公开的审稿意见。'
                 "可能是该 venue 尚未公开评审，或需要 <code>--token</code> 访问权限。</p>")

    orphans = ""
    if paper.orphan_comments:
        orphans = (
            '<section class="orphans"><h3>不属于单篇审稿的公开讨论</h3>'
            + "".join(_message_html(c, 0) for c in paper.orphan_comments)
            + "</section>"
        )

    withdrawn = '<div class="warn warn-strong">🚫 该投稿已撤稿。</div>' if paper.withdrawn else ""
    tldr = f'<div class="tldr"><strong>TL;DR</strong> {_e(paper.tldr)}</div>' if paper.tldr else ""

    toc = "".join(
        f'<li><a href="#{_anchor(r.label)}">{_e(r.label)}</a></li>' for r in paper.reviews
    )

    return (
        f'<section class="paper{" active" if active else ""}" id="paper-{index}" '
        f'data-index="{index}" data-title="{_e(paper.title)}">{withdrawn}{warnings}'
        f'<header class="paper-head"><h1>{_e(paper.title)}</h1>{tldr}'
        f'<div class="meta-grid">{meta_html}</div></header>'
        f"{abstract}{meta_review}{decision}"
        f'<section class="overview"><h3>审稿人对照总览</h3>'
        f'<p class="legend">评分底色为<b>本篇论文内部的相对高低</b>：越绿越宽松，越红越严格。'
        f"不同会议的评分量纲不同（ICLR 1–10、NeurIPS 1–6、ICML 1–5 等），故采用相对着色。</p>"
        f"{_summary_table(paper)}</section>"
        f'<section class="reviews"><h3>审稿人与讨论详情</h3>{cards}</section>'
        f"{orphans}"
        f'<nav class="paper-toc"><h4>本篇目录</h4><ol>{toc}</ol></nav>'
        f"</section>"
    )


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #
CSS = """
/* ---------------------------------------------------------------------------
   Design tokens. Every colour, radius, shadow and key size is named once, so
   the page reads as one system instead of a pile of one-off values. Dark mode
   swaps the same token names, so no component needs to know about it.
   --------------------------------------------------------------------------- */
:root{
  --bg:#f5f6f8;
  --surface:#ffffff;
  --sunken:#f9fafb;
  --hover:#f3f5f8;
  --border:#e4e7ec;
  --border-strong:#d5d9e0;
  --text:#101828;
  --text-2:#475467;
  --text-3:#667085;
  --accent:#3b5bdb;
  --accent-weak:#eef1fe;
  --accent-border:#c7d0fb;
  --ok:#067647;      --ok-bg:#ecfdf3;      --ok-border:#abefc6;
  --warn:#b54708;    --warn-bg:#fffaeb;    --warn-border:#fdecc8;
  --danger:#b42318;  --danger-bg:#fef3f2;  --danger-border:#fecdca;
  --radius-sm:6px; --radius-md:10px; --radius-lg:14px; --radius-pill:999px;
  --shadow-sm:0 1px 2px rgba(16,24,40,.06);
  --shadow-md:0 6px 16px -6px rgba(16,24,40,.14), 0 2px 6px -2px rgba(16,24,40,.06);
  --topbar-h:56px;
  --sidebar-w:268px;
  --measure:78ch;
  --font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei UI",
    "PingFang SC","Hiragino Sans GB",Roboto,Helvetica,Arial,sans-serif;
  --font-mono:ui-monospace,SFMono-Regular,"SF Mono",Consolas,"Liberation Mono",
    Menlo,monospace;
}
[data-theme="dark"]{
  --bg:#0f1115;
  --surface:#171a20;
  --sunken:#1c2027;
  --hover:#22262e;
  --border:#2a2f38;
  --border-strong:#3a404b;
  --text:#e8eaee;
  --text-2:#a9b1bd;
  --text-3:#7d8794;
  --accent:#8ea2ff;
  --accent-weak:#1b2136;
  --accent-border:#333e68;
  --ok:#5cd39a;      --ok-bg:#10251b;      --ok-border:#1f4433;
  --warn:#e2a33c;    --warn-bg:#2a2110;    --warn-border:#4a3a17;
  --danger:#f28b82;  --danger-bg:#2a1614;  --danger-border:#4d2723;
  --shadow-sm:0 1px 2px rgba(0,0,0,.45);
  --shadow-md:0 8px 22px -8px rgba(0,0,0,.6), 0 2px 6px -2px rgba(0,0,0,.4);
}

/* ---------------------------------------------------------------------------
   Base
   --------------------------------------------------------------------------- */
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.7 var(--font-sans);
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none;text-underline-offset:2px}
a:hover{text-decoration:underline}
h1,h2,h3,h4,h5{text-wrap:balance;letter-spacing:-.011em}
code{background:var(--sunken);border:1px solid var(--border);
  padding:.08em .38em;border-radius:var(--radius-sm);font-size:.86em;
  font-family:var(--font-mono)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:var(--radius-sm)}
/* The sticky header would otherwise cover whatever an anchor jumps to. */
[id]{scroll-margin-top:calc(var(--topbar-h) + 14px)}

/* ---------------------------------------------------------------------------
   Top bar
   --------------------------------------------------------------------------- */
.topbar{position:sticky;top:0;z-index:50;display:flex;flex-wrap:wrap;gap:14px;
  align-items:center;min-height:var(--topbar-h);padding:8px 22px;
  /* Opaque first: color-mix() is recent, and an unsupported value would leave
     the sticky bar transparent with the page scrolling through it. */
  background:var(--surface);
  background:color-mix(in srgb,var(--surface) 88%,transparent);
  -webkit-backdrop-filter:saturate(180%) blur(12px);
  backdrop-filter:saturate(180%) blur(12px);
  border-bottom:1px solid var(--border)}
.brand{font-weight:650;font-size:15px;white-space:nowrap;display:flex;
  align-items:baseline;gap:10px}
.brand span{color:var(--text-3);font-weight:400;font-size:12.5px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;flex:1;min-width:180px}
.tab{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  padding:6px 13px;border-radius:var(--radius-pill);border:1px solid var(--border);
  background:var(--surface);font-size:13px;cursor:pointer;color:var(--text-2);
  transition:background .12s,border-color .12s,color .12s}
.tab:hover{background:var(--hover);color:var(--text)}
.tab.active{background:var(--accent-weak);border-color:var(--accent-border);
  color:var(--accent);font-weight:600}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tools input[type=search]{padding:7px 13px;border-radius:var(--radius-md);
  border:1px solid var(--border);font-size:13px;min-width:210px;
  background:var(--surface);color:var(--text);font-family:inherit}
.tools input[type=search]::placeholder{color:var(--text-3)}
.tools input[type=search]:focus{border-color:var(--accent);outline:none;
  box-shadow:0 0 0 3px var(--accent-weak)}
.chips{display:flex;gap:0;border:1px solid var(--border);
  border-radius:var(--radius-md);overflow:hidden;background:var(--surface)}
.chip-btn{padding:6px 12px;border:0;border-right:1px solid var(--border);
  background:transparent;font-size:12.5px;cursor:pointer;color:var(--text-2);
  font-family:inherit}
.chip-btn:last-child{border-right:0}
.chip-btn:hover{background:var(--hover);color:var(--text)}
.chip-btn.active{background:var(--accent);color:#fff;font-weight:600}
[data-theme="dark"] .chip-btn.active{color:#0f1115}
.btn{padding:6px 12px;border-radius:var(--radius-md);border:1px solid var(--border);
  background:var(--surface);font-size:12.5px;cursor:pointer;color:var(--text-2);
  font-family:inherit;transition:background .12s,border-color .12s,color .12s}
.btn:hover{border-color:var(--border-strong);background:var(--hover);color:var(--text)}
.btn.active{background:var(--accent-weak);border-color:var(--accent-border);
  color:var(--accent);font-weight:600}

/* ---------------------------------------------------------------------------
   Layout
   --------------------------------------------------------------------------- */
.layout{display:grid;grid-template-columns:var(--sidebar-w) minmax(0,1fr);
  gap:28px;max-width:1440px;margin:0 auto;padding:24px 22px 90px}
aside.side{position:sticky;top:calc(var(--topbar-h) + 18px);align-self:start;
  max-height:calc(100vh - var(--topbar-h) - 40px);overflow:auto;font-size:13px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-lg);padding:16px;box-shadow:var(--shadow-sm)}
aside.side h4{margin:0 0 9px;font-size:11px;text-transform:uppercase;
  letter-spacing:.07em;color:var(--text-3);font-weight:650}
aside.side h4 + *{margin-top:0}
aside.side ol,aside.side ul{margin:0 0 18px;padding-left:19px}
aside.side li{margin:4px 0}
aside.side ol.papers{padding-left:19px}
aside.side .pitem{margin-bottom:6px;line-height:1.5}
aside.side .pitem > a{font-weight:550}
aside.side .toc-host ol{padding-left:17px}
aside.side .toc-host a{color:var(--text-2)}
aside.side .toc-host a:hover{color:var(--accent)}
main{min-width:0}

/* ---------------------------------------------------------------------------
   Paper header
   --------------------------------------------------------------------------- */
.paper{display:none}
.paper.active{display:block;animation:fade .18s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.paper-head{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-lg);padding:22px 24px;margin-bottom:18px;
  box-shadow:var(--shadow-sm)}
.paper-head h1{margin:0 0 14px;font-size:23px;line-height:1.34;font-weight:680}
.tldr{background:var(--accent-weak);border-left:3px solid var(--accent);
  padding:9px 14px;border-radius:0 var(--radius-md) var(--radius-md) 0;
  font-size:14px;margin:0 0 16px;color:var(--text)}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:10px 24px}
.meta-item{display:flex;gap:10px;font-size:13.5px;align-items:baseline}
.mk{color:var(--text-3);min-width:84px;flex-shrink:0;font-size:12.5px}
.mv{min-width:0;overflow-wrap:anywhere}
.decision{color:var(--ok)}

/* ---------------------------------------------------------------------------
   Cards and disclosures
   --------------------------------------------------------------------------- */
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-lg);margin-bottom:12px;overflow:hidden;
  box-shadow:var(--shadow-sm)}
details.card > summary{padding:13px 18px;cursor:pointer;display:flex;gap:10px;
  align-items:center;flex-wrap:wrap;list-style:none;font-size:14px;
  transition:background .12s}
details.card > summary:hover{background:var(--hover)}
details.card > summary::-webkit-details-marker{display:none}
details.card > summary::before{content:"";width:0;height:0;flex-shrink:0;
  border-left:5px solid var(--text-3);border-top:4px solid transparent;
  border-bottom:4px solid transparent;transition:transform .15s}
details.card[open] > summary::before{transform:rotate(90deg)}
.card-meta{display:flex;gap:16px;flex-wrap:wrap;padding:0 18px 11px;
  font-size:12.5px;color:var(--text-3)}
.card > .body,.card > .field,.card > section{padding:0 18px 16px}
.spacer{flex:1}
.rlabel{font-weight:620}
time{color:var(--text-3);font-size:12.5px;white-space:nowrap}

/* ---------------------------------------------------------------------------
   Badges and chips
   --------------------------------------------------------------------------- */
.badge{display:inline-block;padding:2px 10px;border-radius:var(--radius-pill);
  font-size:12.5px;border:1px solid transparent;font-weight:620;
  white-space:nowrap;font-variant-numeric:tabular-nums}
.badge-plain{background:var(--sunken);color:var(--text-2);
  border-color:var(--border)}
.badge-ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-border)}
.badge-warn{background:var(--warn-bg);color:var(--warn);border-color:var(--warn-border)}
.chip{display:inline-block;padding:1px 8px;border-radius:var(--radius-sm);
  background:var(--sunken);border:1px solid var(--border);font-size:12.5px}
.chip-empty{color:var(--text-3)}

/* ---------------------------------------------------------------------------
   Section headings and the reviewer table
   --------------------------------------------------------------------------- */
.overview h3,.reviews h3,.orphans h3{font-size:16px;font-weight:650;
  margin:30px 0 10px}
.legend{font-size:12.5px;color:var(--text-3);margin:0 0 12px;max-width:var(--measure)}
table.grid{width:100%;border-collapse:separate;border-spacing:0;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-lg);overflow:hidden;font-size:13px;
  box-shadow:var(--shadow-sm)}
table.grid th{background:var(--sunken);text-align:left;padding:10px 12px;
  font-size:11.5px;color:var(--text-3);font-weight:650;white-space:nowrap;
  border-bottom:1px solid var(--border);text-transform:uppercase;
  letter-spacing:.03em}
table.grid td{padding:10px 12px;border-top:1px solid var(--border);
  vertical-align:top}
table.grid tbody tr:hover td{background:var(--hover)}
.tiny{font-size:11.5px;color:var(--text-3);margin-top:3px;line-height:1.45}

/* ---------------------------------------------------------------------------
   Review cards
   --------------------------------------------------------------------------- */
.review-card{margin-bottom:16px}
.review-card > summary{background:var(--sunken)}
.review-card[open] > summary{border-bottom:1px solid var(--border)}
.rating-meaning{padding:0 18px;font-size:13px;color:var(--text-2);margin:3px 0}
.qa-set,.full,.thread{padding:8px 18px 18px;border-top:1px dashed var(--border);
  margin-top:10px}
.qa-set h4,.full h4,.thread h4{font-size:12px;font-weight:650;margin:16px 0 9px;
  color:var(--text-3);text-transform:uppercase;letter-spacing:.05em}

/* ---------------------------------------------------------------------------
   Question / answer pairs
   --------------------------------------------------------------------------- */
.qa{border:1px solid var(--border);border-radius:var(--radius-md);
  margin-bottom:12px;overflow:hidden;background:var(--surface)}
.question{background:var(--sunken);padding:12px 14px;display:flex;gap:10px;
  font-size:14px;align-items:flex-start}
.qtag{flex-shrink:0;font-weight:700;color:var(--accent);font-size:11.5px;
  background:var(--accent-weak);border:1px solid var(--accent-border);
  border-radius:var(--radius-sm);padding:1px 7px;margin-top:3px;
  font-variant-numeric:tabular-nums}
.answer{padding:12px 14px;border-top:1px solid var(--border)}
.answer-head{font-size:11.5px;color:var(--ok);margin-bottom:6px;font-weight:650;
  display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.answer-missing{padding:12px 14px;color:var(--text-3);font-size:13px;
  font-style:italic;border-top:1px solid var(--border)}

/* ---------------------------------------------------------------------------
   Rendered field content
   --------------------------------------------------------------------------- */
.field{margin-bottom:16px}
.field h5{margin:0 0 5px;font-size:12px;color:var(--text-3);font-weight:650;
  text-transform:uppercase;letter-spacing:.04em}
.body{overflow-wrap:anywhere;font-size:14.5px;max-width:var(--measure)}
.body.muted{color:var(--text-3)}
pre.raw-body{white-space:pre-wrap;margin:0;font-family:var(--font-mono);
  font-size:12.5px;line-height:1.65;background:var(--sunken);
  border:1px solid var(--border);border-radius:var(--radius-md);
  padding:12px 14px;color:var(--text-2);max-width:var(--measure)}
/* Rendered review Markdown, kept visually subordinate to the report's own
   structure so a review's headings never compete with the page headings. */
.md-body{font-size:14.5px;line-height:1.78;max-width:var(--measure)}
.md-body > *:first-child{margin-top:0}
.md-body > *:last-child{margin-bottom:0}
.md-body p{margin:0 0 11px}
.md-body h3,.md-body h4,.md-body h5,.md-body h6{margin:18px 0 7px;
  font-size:14px;color:var(--text);font-weight:650}
.md-body ul,.md-body ol{margin:7px 0 11px;padding-left:24px}
.md-body li{margin:4px 0}
.md-body li > ul,.md-body li > ol{margin:4px 0}
.md-body blockquote{margin:10px 0;padding:2px 0 2px 14px;
  border-left:3px solid var(--border-strong);color:var(--text-2)}
.md-body blockquote p{margin:0 0 7px}
.md-body code{font-size:12.8px}
.md-body pre{background:var(--sunken);border:1px solid var(--border);
  border-radius:var(--radius-md);padding:12px 14px;overflow:auto;margin:10px 0}
.md-body pre code{background:none;border:0;padding:0;font-size:12.5px}
.md-body hr{border:none;border-top:1px solid var(--border);margin:16px 0}
.md-body a{word-break:break-all}
.md-body table.md-table{border-collapse:separate;border-spacing:0;margin:10px 0;
  font-size:13px;border:1px solid var(--border);
  border-radius:var(--radius-md);overflow:hidden}
.md-body table.md-table th,.md-body table.md-table td{
  border-bottom:1px solid var(--border);border-right:1px solid var(--border);
  padding:6px 10px}
.md-body table.md-table th{background:var(--sunken);font-weight:650}
.md-body table.md-table tr:last-child td{border-bottom:0}
.md-body table.md-table th:last-child,.md-body table.md-table td:last-child{
  border-right:0}

/* Typeset maths, via browser-native MathML produced by orcr.mathrender.
   Deliberately no font-family override: MathML needs a font with a MATH table
   so that radicals, fractions and stretchy operators lay out correctly, and
   naming a non-maths font here would break exactly that. No KaTeX, no MathJax,
   no web fonts, no CDN -- the report stays a single self-contained file. */
.md-body .math{font-size:1.05em;line-height:1.45;color:var(--text)}
.md-body .math-display{display:block;text-align:center;margin:16px 0;
  overflow-x:auto;overflow-y:hidden;padding:2px 0}
.md-body .math-display math{display:block;margin:0 auto}
/* A formula the renderer refused to guess at: shown as source, on purpose. */
.md-body .math.math-raw{white-space:pre-wrap;background:var(--sunken);
  border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:1px 5px;font-family:var(--font-mono);font-size:.85em;
  color:var(--text-2);line-height:1.55}

/* ---------------------------------------------------------------------------
   Discussion thread
   --------------------------------------------------------------------------- */
.msg{border-left:3px solid var(--border);padding:9px 0 5px 15px;
  margin:11px 0 11px calc(var(--depth,0) * 18px)}
.msg-author{border-left-color:#6fbf8f}
.msg-comment{border-left-color:var(--border-strong)}
.msg-meta{border-left-color:#9b8cf0}
.msg-decision{border-left-color:#e0b050}
[data-theme="dark"] .msg-author{border-left-color:#3f7d5b}
[data-theme="dark"] .msg-meta{border-left-color:#5f55a8}
[data-theme="dark"] .msg-decision{border-left-color:#8a6a24}
.msg-head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;
  margin-bottom:6px}
.msg-head .who{font-weight:620;font-size:13.5px}
.msg-head .role{font-size:11.5px;color:var(--text-3);background:var(--sunken);
  border:1px solid var(--border);padding:1px 7px;border-radius:var(--radius-sm)}
.msg-head .src{font-size:12px}
.kv-list{margin-top:9px}
.kv{margin-bottom:9px}
.kv .k{display:block;font-size:11.5px;color:var(--text-3);font-weight:650;
  margin-bottom:3px;text-transform:uppercase;letter-spacing:.04em}
.note{color:var(--text-3);font-size:13px;max-width:var(--measure)}
.orphans .msg{margin-bottom:14px}

/* ---------------------------------------------------------------------------
   Callouts
   --------------------------------------------------------------------------- */
.warn{background:var(--warn-bg);border:1px solid var(--warn-border);
  color:var(--warn);padding:10px 14px;border-radius:var(--radius-md);
  margin-bottom:12px;font-size:13.5px}
.warn-strong{background:var(--danger-bg);border-color:var(--danger-border);
  color:var(--danger);font-weight:620}
.copy{padding:3px 10px;border-radius:var(--radius-sm);border:1px solid var(--border);
  background:var(--surface);font-size:11.5px;cursor:pointer;color:var(--text-2);
  font-family:inherit;transition:background .12s,color .12s}
.copy:hover{border-color:var(--accent-border);background:var(--accent-weak);
  color:var(--accent)}
mark{background:#fff2a8;color:#3a2c00;padding:0 2px;border-radius:3px}
[data-theme="dark"] mark{background:#6b5700;color:#ffeaa0}
footer{max-width:1440px;margin:0 auto;padding:8px 22px 60px;
  color:var(--text-3);font-size:12.5px;line-height:1.7}
footer p{margin:0 0 6px;max-width:var(--measure)}
.hidden{display:none !important}
.paper-toc{display:none}
body.raw-mode .md-body{display:none}
body.raw-mode pre.raw-body{display:block !important}

/* ---------------------------------------------------------------------------
   Responsive and print
   --------------------------------------------------------------------------- */
@media (max-width:1040px){
  .layout{grid-template-columns:1fr;padding:18px 16px 70px}
  aside.side{display:none}
  .tools input[type=search]{min-width:0;flex:1}
}
@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  .paper.active{animation:none}
}
@media print{
  .topbar,aside.side,.copy,.chips{display:none}
  .paper{display:block !important}
  body{background:#fff;color:#000;font-size:11pt}
  .card,.paper-head,table.grid{box-shadow:none;break-inside:avoid}
  details.card{break-inside:avoid}
  a{color:#000;text-decoration:underline}
  .md-body,.body{max-width:none}
}
"""

JS = """
(function(){
  var searchBox=document.getElementById('search');
  var tabs=[].slice.call(document.querySelectorAll('.tab'));
  var papers=[].slice.call(document.querySelectorAll('.paper'));

  function activePaper(){return document.querySelector('.paper.active');}

  function clearMarks(root){
    [].slice.call(root.querySelectorAll('mark')).forEach(function(m){
      var t=document.createTextNode(m.textContent);
      m.parentNode.replaceChild(t,m);
    });
    root.normalize();
  }

  function markMatches(root,term){
    if(!term) return;
    var walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{
      acceptNode:function(node){
        if(!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var p=node.parentNode;
        if(!p||/^(SCRIPT|STYLE|MARK|BUTTON)$/.test(p.nodeName)) return NodeFilter.FILTER_REJECT;
        // Never mark inside the hidden original-text copies: a hit there would
        // reveal a message that has no visible match.
        if(p.closest&&p.closest('.raw-body')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes=[],n;
    while(n=walker.nextNode()) nodes.push(n);
    var lower=term.toLowerCase();
    nodes.forEach(function(node){
      var text=node.nodeValue, idx=text.toLowerCase().indexOf(lower);
      if(idx<0) return;
      var frag=document.createDocumentFragment(), cursor=0;
      while(idx>=0){
        if(idx>cursor) frag.appendChild(document.createTextNode(text.slice(cursor,idx)));
        var m=document.createElement('mark');
        m.textContent=text.slice(idx,idx+term.length);
        frag.appendChild(m);
        cursor=idx+term.length;
        idx=text.toLowerCase().indexOf(lower,cursor);
      }
      if(cursor<text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
      node.parentNode.replaceChild(frag,node);
    });
  }

  var filter='all';
  function applyFilter(){
    var paper=activePaper(); if(!paper) return;
    [].slice.call(paper.querySelectorAll('[data-kind]')).forEach(function(el){
      var kind=el.getAttribute('data-kind');
      var show = filter==='all' || kind===filter
        || (filter==='review' && (kind==='review'||kind==='meta_review'||kind==='decision'));
      if(el.tagName==='DETAILS' && el.classList.contains('card')){
        el.classList.toggle('hidden',!show);
        if(show && filter!=='all') el.open=true;
      } else {
        el.classList.toggle('hidden',!show);
      }
    });
  }

  [].slice.call(document.querySelectorAll('.chip-btn')).forEach(function(btn){
    btn.addEventListener('click',function(){
      [].slice.call(document.querySelectorAll('.chip-btn')).forEach(function(b){b.classList.remove('active');});
      btn.classList.add('active');
      filter=btn.getAttribute('data-filter');
      applyFilter();
    });
  });

  if(searchBox){
    searchBox.addEventListener('input',function(){
      var term=searchBox.value.trim();
      papers.forEach(function(p){clearMarks(p);});
      if(!term){applyFilter();return;}
      var paper=activePaper(); if(!paper) return;
      // Search the whole paper; reveal anything that matches.
      [].slice.call(paper.querySelectorAll('details.card')).forEach(function(d){d.classList.add('hidden');});
      [].slice.call(paper.querySelectorAll('.msg')).forEach(function(m){m.classList.remove('hidden');});
      markMatches(paper,term);
      [].slice.call(paper.querySelectorAll('details.card')).forEach(function(d){
        var hit=d.querySelector('mark');
        if(hit){d.classList.remove('hidden');d.open=true;}
      });
      [].slice.call(paper.querySelectorAll('.msg')).forEach(function(m){
        if(!m.querySelector('mark')){
          // keep a message visible when one of its children matched
          if(!m.querySelector('mark')) m.classList.add('hidden');
        }
      });
    });
  }

  function activate(index){
    papers.forEach(function(p){p.classList.toggle('active',p.getAttribute('data-index')===String(index));});
    tabs.forEach(function(t){t.classList.toggle('active',t.getAttribute('data-index')===String(index));});
    if(searchBox){searchBox.value='';}
    papers.forEach(function(p){clearMarks(p);});
    filter='all';
    [].slice.call(document.querySelectorAll('.chip-btn')).forEach(function(b){
      b.classList.toggle('active',b.getAttribute('data-filter')==='all');
    });
    [].slice.call(document.querySelectorAll('[data-kind]')).forEach(function(el){el.classList.remove('hidden');});
    window.scrollTo({top:0});
    buildSideToc();
  }
  tabs.forEach(function(t){
    t.addEventListener('click',function(){activate(t.getAttribute('data-index'));});
  });

  var expandAll=document.getElementById('expandAll');
  if(expandAll){
    var opened=true;
    expandAll.addEventListener('click',function(){
      opened=!opened;
      var paper=activePaper(); if(!paper) return;
      [].slice.call(paper.querySelectorAll('details.card')).forEach(function(d){d.open=opened;});
      expandAll.textContent=opened?'全部折叠':'全部展开';
    });
  }

  // Reviews are written in Markdown and rendered; the original text stays in
  // the page so this toggle can always show exactly what OpenReview stores.
  var rawToggle=document.getElementById('rawToggle');
  if(rawToggle){
    rawToggle.addEventListener('click',function(){
      var on=document.body.classList.toggle('raw-mode');
      rawToggle.textContent=on?'显示渲染版':'显示原文';
      rawToggle.classList.toggle('active',on);
    });
  }

  // Theme follows the OS unless the reader has chosen explicitly, in which
  // case the choice is remembered for this browser.
  var themeToggle=document.getElementById('themeToggle');
  if(themeToggle){
    themeToggle.addEventListener('click',function(){
      var next=document.documentElement.dataset.theme==='dark'?'light':'dark';
      document.documentElement.dataset.theme=next;
      try{localStorage.setItem('orcr-theme',next);}catch(e){}
    });
  }

  document.addEventListener('click',function(ev){
    var btn=ev.target.closest?ev.target.closest('.copy'):null;
    if(!btn) return;
    ev.preventDefault();
    var src=document.getElementById(btn.getAttribute('data-copy'));
    if(!src) return;
    var text=src.textContent;
    var done=function(){btn.textContent='已复制';setTimeout(function(){btn.textContent='复制';},1400);};
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(done,function(){done();});
    } else {
      var ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);
      ta.select();try{document.execCommand('copy');}catch(e){}
      document.body.removeChild(ta);done();
    }
  });

  function buildSideToc(){
    var paper=activePaper(); if(!paper) return;
    var host=document.querySelector('.side .toc-host'); if(!host) return;
    host.innerHTML='';
    var h4=document.createElement('h4');h4.textContent='审稿人';host.appendChild(h4);
    var ol=document.createElement('ol');
    [].slice.call(paper.querySelectorAll('.review-card')).forEach(function(card){
      var label=card.querySelector('.rlabel');
      if(!label) return;
      var li=document.createElement('li');var a=document.createElement('a');
      a.textContent=label.textContent;
      var section=card.closest('section.reviews');
      var target=card;
      a.addEventListener('click',function(e){
        e.preventDefault();
        var d=target.closest('details'); if(d) d.open=true;
        target.scrollIntoView({behavior:'smooth',block:'start'});
      });
      li.appendChild(a);ol.appendChild(li);
    });
    host.appendChild(ol);
  }
  buildSideToc();
})();
"""


def render_report(papers: list[Paper], generated_at: float | None = None,
                  title: str | None = None) -> str:
    """Render one self-contained HTML page covering every crawled paper."""
    if not papers:
        raise ValueError("no papers to render")

    stamp = datetime.fromtimestamp(generated_at or time.time()).strftime("%Y-%m-%d %H:%M")
    page_title = title or (
        papers[0].title if len(papers) == 1 else f"OpenReview 审稿报告（{len(papers)} 篇）"
    )

    tabs = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-index="{i}" '
        f'title="{_e(p.title)}">{_e(p.title[:52])}</button>'
        for i, p in enumerate(papers)
    )
    sections = "".join(
        _paper_section(p, i, active=(i == 0)) for i, p in enumerate(papers)
    )
    sidebar_papers = "".join(
        f'<li class="pitem"><a href="#paper-{i}" data-index="{i}">{_e(p.title[:44])}'
        f"</a></li>"
        for i, p in enumerate(papers)
    )

    subtitle = (
        f"{len(papers)} 篇论文 · 生成于 {stamp}"
        if len(papers) > 1
        else f"{_e(papers[0].venue) or 'OpenReview'} · 生成于 {stamp}"
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(page_title)} — OpenReview 审稿报告</title>
<script>
/* Resolve the theme before first paint. Without this the page would flash
   light and then switch, because the preference is only known once the
   stylesheet and script below have run. */
(function(){{
  try{{
    var saved=localStorage.getItem('orcr-theme');
    var dark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.theme=saved||(dark?'dark':'light');
  }}catch(e){{document.documentElement.dataset.theme='light';}}
}})();
</script>
<style>{CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="brand">OpenReview 审稿报告<span>{subtitle}</span></div>
  <nav class="tabs">{tabs}</nav>
  <div class="tools">
    <input id="search" type="search" placeholder="全文搜索（当前论文）…" autocomplete="off">
    <div class="chips">
      <button class="chip-btn active" data-filter="all">全部</button>
      <button class="chip-btn" data-filter="review">审稿意见</button>
      <button class="chip-btn" data-filter="author_response">作者回复</button>
      <button class="chip-btn" data-filter="comment">讨论</button>
    </div>
    <button class="btn" id="rawToggle">显示原文</button>
    <button class="btn" id="expandAll">全部折叠</button>
    <button class="btn" id="themeToggle" title="切换深色 / 浅色主题"
            aria-label="切换深色或浅色主题">◐</button>
  </div>
</header>
<div class="layout">
  <aside class="side">
    <h4>论文</h4>
    <ol class="papers">{sidebar_papers}</ol>
    <div class="toc-host"></div>
  </aside>
  <main>{sections}</main>
</div>
<footer>
  <p>由 <code>openreview-crawler</code> 生成 · 数据来源 OpenReview API (api2) ·
  报告中所有内容版权归原作者与审稿人所有，仅供研究参考。</p>
  <p>审稿人身份按 OpenReview 的匿名规则原样呈现；本工具不会也不能绕过匿名机制。</p>
</footer>
<script>{JS}</script>
</body>
</html>
"""
