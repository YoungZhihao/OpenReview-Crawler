"""Markdown renderer.

Produces one self-contained ``.md`` per paper, plus a combined index when
several papers are crawled at once. The layout is deliberately "scan first,
read later": a metadata block, a reviewer comparison table, then the full
threads.
"""

from __future__ import annotations

import time
from datetime import datetime

from .extract import (
    NON_CONTENT_KINDS,
    Message,
    Paper,
    order_fields,
    parse_score,
    split_questions,
)
from .qa import build_qa

KIND_ICON = {
    "review": "🔍",
    "meta_review": "🧭",
    "decision": "⚖️",
    "author_response": "✍️",
    "comment": "💬",
    "withdrawal": "🚫",
    "desk_reject": "🚫",
}


def _fmt_dt(millis: int) -> str:
    if not millis:
        return "—"
    try:
        return datetime.fromtimestamp(millis / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def _rating_bar(value: float | None, low: float = 1, high: float = 10, width: int = 10) -> str:
    if value is None:
        return ""
    fraction = max(0.0, min(1.0, (value - low) / (high - low)))
    filled = round(fraction * width)
    return "`" + "█" * filled + "░" * (width - filled) + "`"


def _indent_block(text: str, prefix: str = "> ") -> str:
    lines = (text or "").split("\n")
    return "\n".join(prefix + line if line.strip() else prefix.rstrip() for line in lines)


def _message_block(message: Message, level: int, depth_marker: str = "") -> str:
    """Render one note in a discussion thread."""
    icon = KIND_ICON.get(message.kind, "💬")
    heading = "#" * min(6, level)
    who = f"{depth_marker}{message.label or message.role or 'Anonymous'}"
    meta = [f"`{message.created_str or '时间未知'}`"]
    if message.role and message.role not in who:
        meta.append(message.role_zh)
    if message.url:
        meta.append(f"[原文]({message.url})")

    out = [f"{heading} {icon} {who}", "", " · ".join(meta), ""]
    body = message.field("rebuttal", "response", "comment") or ""
    if not body:
        body = max(message.fields.values(), key=len) if message.fields else ""
    out.append(_indent_block(body or "（无正文）"))
    out.append("")
    return "\n".join(out)


def render_paper(paper: Paper, include_abstract: bool = True) -> str:
    """Render a single paper to Markdown."""
    lines: list[str] = []
    add = lines.append

    add(f"# {paper.title}")
    add("")

    # ---- metadata ---------------------------------------------------------- #
    mean = paper.mean_rating
    add("| 项目 | 内容 |")
    add("| --- | --- |")
    add(f"| 会议 / Venue | {paper.venue or '—'} |")
    add(f"| OpenReview | [{paper.forum_id}]({paper.forum_url}) |")
    add(f"| PDF | [openreview.net/pdf]({paper.pdf_url}) |")
    if paper.authors:
        add(f"| 作者 | {', '.join(paper.authors)} |")
    if paper.primary_area:
        add(f"| 领域 | {paper.primary_area} |")
    if paper.keywords:
        add(f"| 关键词 | {', '.join(paper.keywords)} |")
    add(f"| 审稿人数 | {len(paper.reviews)} |")
    if paper.ratings:
        add(f"| 评分 | 平均 **{mean:.2f}** ({paper.rating_spread}) "
            f"{_rating_bar(mean)} |")
    if paper.decision_text:
        add(f"| 最终决定 | **{paper.decision_text}** |")
    add(f"| 抓取时间 | {_fmt_dt(int(time.time() * 1000))} |")
    add("")

    if paper.tldr:
        add(f"> **TL;DR** {paper.tldr}")
        add("")

    for warning in paper.warnings:
        add(f"> ⚠️ {warning}")
        add("")

    # ---- contents ---------------------------------------------------------- #
    add("## 目录")
    add("")
    add("- [审稿人对照总览](#审稿人对照总览)")
    if paper.abstract and include_abstract:
        add("- [摘要](#摘要)")
    if paper.meta_reviews:
        add("- [元审稿意见 Meta Review](#元审稿意见-meta-review)")
    if paper.decisions:
        add("- [最终决定](#最终决定)")
    for review in paper.reviews:
        anchor = review.label.replace(" ", "-").replace("#", "").replace("(", "").replace(")", "")
        add(f"- [{review.label}](#{anchor.lower()})")
    if paper.orphan_comments:
        add("- [不属于单篇审稿的公开讨论](#不属于单篇审稿的公开讨论)")
    if paper.withdrawn:
        add("")
        add("> 🚫 **该投稿已撤稿。**")
    add("")

    # ---- reviewer table ---------------------------------------------------- #
    add("## 审稿人对照总览")
    add("")
    rows = paper.reviewer_rows()
    if not rows:
        add("_未抓取到审稿意见。_")
        add("")
    else:
        add("| 审稿人 | 匿名 ID | 评分 | 置信度 | Soundness | Presentation | Contribution | 问题数 | 回复数 | 作者已回复 | 提交时间 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in rows:
            rating = row["rating"]
            if row["rating_meaning"]:
                rating = f"**{rating}** — {row['rating_meaning']}"
            confidence = row["confidence"]
            if row["confidence_meaning"]:
                confidence = f"{confidence} — {row['confidence_meaning']}"
            add(
                f"| {row['reviewer']} | `{row['id']}` | {rating} | {confidence} | "
                f"{row['soundness']} | {row['presentation']} | {row['contribution']} | "
                f"{row['questions']} | {row['followups']} | {row['author_replied']} | "
                f"{row['posted']} |"
            )
        add("")
        if mean is not None:
            add(f"**平均评分 {mean:.2f}**（{paper.rating_spread}）")
            add("")

    # ---- abstract ---------------------------------------------------------- #
    if paper.abstract and include_abstract:
        add("## 摘要")
        add("")
        add(paper.abstract)
        add("")

    # ---- meta review / decision -------------------------------------------- #
    for meta in paper.meta_reviews:
        add("## 元审稿意见 Meta Review")
        add("")
        add(f"`{meta.created_str}` · {meta.role_zh} · [原文]({meta.url})")
        add("")
        for _, label, value in order_fields(meta.fields, "meta_review"):
            add(f"### {label}")
            add("")
            add(value)
            add("")

    for decision in paper.decisions:
        add("## 最终决定")
        add("")
        add(f"`{decision.created_str}` · {decision.role_zh} · [原文]({decision.url})")
        add("")
        for _, label, value in order_fields(decision.fields, "decision"):
            add(f"### {label}")
            add("")
            add(value)
            add("")

    # ---- reviews ------------------------------------------------------------ #
    if paper.reviews:
        add("## 审稿人与讨论详情")
        add("")

    for review in paper.reviews:
        add(f"### {review.label}")
        add("")
        head = [f"`{review.created_str}`"]
        if review.role:
            head.append(review.role_zh)
        if review.anon_id:
            head.append(f"匿名 ID `{review.anon_id}`")
        if review.profile_id:
            head.append(f"公开身份 `{review.profile_id}`")
        head.append(f"[在 OpenReview 上查看]({review.url})")
        add(" · ".join(head))
        add("")

        rating, meaning = parse_score(review.field("rating", "overall_assessment"))
        if rating is not None or meaning:
            label = f"**评分 Rating：{rating:g}**" if rating is not None else "**评分 Rating：**"
            add(f"{label} {meaning}")
            add("")

        # -- questions/answers first: this is the part people come for ------ #
        qa = build_qa(review)
        if qa.questions:
            add("#### 审稿问题与作者回复对照")
            add("")
            if qa.note:
                add(f"_{qa.note}_")
                add("")
            for index, item in enumerate(qa.questions, start=1):
                add(f"**Q{index}. {item.question}**")
                add("")
                if item.answered:
                    source = (f" · 对应作者 {item.answer_block}"
                              if item.answer_block else "")
                    add(f"**A{index}**（{item.answer_label} · {item.answer_time}"
                        f"{source}"
                        f"{' · [原文](' + item.answer_url + ')' if item.answer_url else ''}）")
                    add("")
                    add(_indent_block(item.answer))
                    add("")
                else:
                    add("_未找到对应回复。_")
                    add("")

        # -- full review text ---------------------------------------------- #
        add("#### 审稿全文")
        add("")
        for key, label, value in order_fields(review.fields, "review"):
            if key == "questions":
                continue  # already shown above, in paired form
            add(f"**{label}**")
            add("")
            add(value)
            add("")

        # -- the thread under this review ----------------------------------- #
        threads = [c for c in review.children]
        if threads:
            add("#### 讨论串")
            add("")

            def emit(node: Message, depth: int) -> None:
                # Markdown headings stop at level 6, but no message is ever
                # truncated: past that depth the nesting is shown with a "↳"
                # marker instead of a deeper heading.
                if node.kind not in NON_CONTENT_KINDS:
                    add(_message_block(node, 5 + depth, depth_marker="↳ " * depth))
                for child in node.children:
                    emit(child, depth + 1)

            for node in threads:
                emit(node, 0)
        add("---")
        add("")

    # ---- orphan comments ---------------------------------------------------- #
    if paper.orphan_comments:
        add("## 不属于单篇审稿的公开讨论")
        add("")
        for comment in paper.orphan_comments:
            add(_message_block(comment, 3))
        add("")

    return "\n".join(lines).rstrip() + "\n"


def _md_link(target: str) -> str:
    """A Markdown link target, quoted when it contains spaces.

    Report folders are named after paper titles, so paths routinely contain
    spaces; without the angle brackets the link breaks at the first space.
    """
    return f"<{target}>" if " " in target else target


def render_index(papers: list[Paper], generated_at: float | None = None,
                 md_paths: dict[str, str] | None = None) -> str:
    """A cross-paper comparison page -- the point of batch mode."""
    lines: list[str] = []
    add = lines.append
    stamp = datetime.fromtimestamp(generated_at or time.time()).strftime("%Y-%m-%d %H:%M")
    md_paths = md_paths or {}

    add("# OpenReview 抓取汇总")
    add("")
    add(f"共 **{len(papers)}** 篇论文 · 生成于 {stamp}")
    add("")

    completed = [p for p in papers if p.mean_rating is not None]
    if completed:
        overall = sum(p.mean_rating for p in completed) / len(completed)
        add(f"全部论文平均评分 **{overall:.2f}**"
            f"（基于 {len(completed)} 篇有评分的论文）")
        add("")

    add("## 横向对比")
    add("")
    add("| # | 论文 | Venue | 审稿人数 | 平均评分 | 评分区间 | 决定 | 作者回复数 | 文件 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, paper in enumerate(papers, start=1):
        mean = f"{paper.mean_rating:.2f}" if paper.mean_rating is not None else "—"
        responses = len(paper.author_responses)
        md_path = md_paths.get(paper.forum_id)
        if md_path:
            # The HTML sits next to the Markdown under the same stem.
            html_path = md_path[:-3] + ".html"
            links = (f"[MD]({_md_link(md_path)}) · "
                     f"[HTML]({_md_link(html_path)})")
        else:
            links = f"[md]({paper.forum_id}.md)"
        add(
            f"| {i} | {paper.title} | {paper.venue or '—'} | {len(paper.reviews)} | "
            f"{mean} | {paper.rating_spread or '—'} | {paper.decision_text or '—'} | "
            f"{responses} | {links} |"
        )
    add("")

    # Per-reviewer scores across every paper: the fastest way to spot the
    # papers with a split review panel.
    add("## 全部审稿人评分一览")
    add("")
    add("| 论文 | 审稿人 | 评分 | 置信度 | 问题数 | 作者已回复 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for paper in papers:
        for row in paper.reviewer_rows():
            add(
                f"| {paper.title[:60]} | {row['reviewer']} | {row['rating']} | "
                f"{row['confidence']} | {row['questions']} | {row['author_replied']} |"
            )
    add("")

    return "\n".join(lines).rstrip() + "\n"
