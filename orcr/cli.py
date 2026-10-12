"""Command line interface."""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from pathlib import Path

from .api import (
    API2,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    OpenReviewClient,
    OpenReviewError,
    TokenExpired,
)
from .auth import (
    DEFAULT_TOKEN_FILE,
    AuthError,
    console_prompter,
    delete_token_file,
    ensure_token,
    interactive_login,
    jwt_claims,
    load_token_file,
    token_status,
)
from .console import fix_console
from .extract import Paper
from .output import DEFAULT_OUT_DIR, write_reports
from .pipeline import crawl_many
from .render_html import render_report
from .render_md import render_index, render_paper
from .resolve import Candidate, ResolveError, search_papers

BAR = "─" * 78


def _fix_console() -> None:
    """Backwards-compatible alias for the shared helper."""
    fix_console()


def resolve_token(explicit: str | None, token_file: str = DEFAULT_TOKEN_FILE) -> str | None:
    """CLI flag > environment > cached token file.

    Anonymous access works for public venues, but OpenReview serves a
    Cloudflare Turnstile challenge to anonymous traffic that looks automated;
    signing in skips it, which is why the cached token matters even for venues
    that need no privileges to read.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("OPENREVIEW_TOKEN")
    if env:
        return env.strip()
    for candidate in (token_file, str(Path.home() / DEFAULT_TOKEN_FILE)):
        token = load_token_file(candidate)
        if token:
            return token
    return None


def resolve_cookie(explicit: str | None) -> str | None:
    """Browser cookie used to clear the human-check challenge.

    OpenReview serves a bot challenge to IPs making many anonymous requests;
    the challenge sets a cookie in the browser that clears it. Pasting that
    cookie here is the least invasive workaround when waiting is not an option.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("OPENREVIEW_COOKIE")
    if env:
        return env.strip()
    for candidate in (Path.home() / ".openreview_cookie", Path.cwd() / ".openreview_cookie"):
        try:
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl.py",
        description="抓取 OpenReview 论文的审稿人信息、审稿意见、审稿问题与作者回复，"
                    "整理成 Markdown / HTML 报告。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python crawl.py "https://openreview.net/forum?id=xxxxx"
  python crawl.py "Attention Is All You Need" --venue NeurIPS
  python crawl.py "arXiv:2401.12345"
  python crawl.py --input-file papers.txt --out ./output
  python crawl.py "论文标题" --list-candidates      # 只看候选，不抓取
""",
    )
    parser.add_argument("inputs", nargs="*", metavar="PAPER",
                        help="论文的 OpenReview 链接 / forum id / arXiv id / DOI / 标题")
    parser.add_argument("-f", "--input-file", action="append", default=[],
                        metavar="FILE",
                        help="从文本文件读取待抓取列表（每行一条，# 开头为注释）；可重复")

    out = parser.add_argument_group("输出")
    out.add_argument("-o", "--out", default=DEFAULT_OUT_DIR, metavar="DIR",
                     help=f"输出目录（默认 {DEFAULT_OUT_DIR}/）。"
                          "每篇论文在下面单独一个以论文标题命名的子文件夹")
    out.add_argument("--flat", action="store_true",
                     help="不用子文件夹，所有文件直接放在输出目录里（以 forum id 命名）")
    out.add_argument("--formats", default="md,html", metavar="LIST",
                     help="要生成的格式，逗号分隔：md,html（默认两者）")
    out.add_argument("--no-abstract", action="store_true",
                     help="报告中不包含论文摘要")
    out.add_argument("--stdout", action="store_true",
                     help="把 Markdown 直接打印到终端，不写文件")

    pick = parser.add_argument_group("匹配")
    pick.add_argument("--venue", default=None, metavar="TEXT",
                      help="限定会议，如 ICLR / NeurIPS 2023，用于消除同名论文歧义")
    pick.add_argument("--pick", type=int, default=0, metavar="N",
                      help="标题搜索命中多个时，直接选第 N 个候选（默认 0）")
    pick.add_argument("--min-score", type=float, default=0.62, metavar="F",
                      help="标题匹配的最低相似度，低于则报错（默认 0.62）")
    pick.add_argument("-i", "--interactive", action="store_true",
                      help="候选接近时在终端里询问选择哪一个")
    pick.add_argument("--list-candidates", action="store_true",
                      help="只列出搜索候选并退出，不抓取")

    auth = parser.add_argument_group("登录")
    auth.add_argument("--login", action="store_true",
                      help="登录并把令牌保存到 .openreview_token（密码用隐藏输入，不会落盘）")
    auth.add_argument("--logout", action="store_true", help="删除本地保存的令牌")
    auth.add_argument("--email", default=None, metavar="ADDRESS",
                      help="登录邮箱；默认沿用上一次登录用的邮箱。"
                           "也可用环境变量 OPENREVIEW_USER")
    auth.add_argument("--token-file", default=DEFAULT_TOKEN_FILE, metavar="PATH",
                      help=f"令牌保存位置（默认 {DEFAULT_TOKEN_FILE}）")
    auth.add_argument("--no-prompt", action="store_true",
                      help="令牌过期时不询问密码，直接以匿名方式抓取")

    net = parser.add_argument_group("网络与缓存")
    net.add_argument("--token", default=None, metavar="TOKEN",
                     help="直接指定访问令牌（会出现在命令历史里，优先用 --login）。"
                          "也可用环境变量 OPENREVIEW_TOKEN")
    net.add_argument("--cookie", default=None, metavar="COOKIE",
                     help="浏览器 Cookie 字符串，用于通过人机校验；"
                          "也可用环境变量 OPENREVIEW_COOKIE")
    net.add_argument("--cache-dir", default=".cache", metavar="DIR",
                     help="原始响应的缓存目录（默认 .cache）")
    net.add_argument("--no-cache", action="store_true", help="完全禁用缓存")
    net.add_argument("--refresh", action="store_true", help="忽略缓存，重新抓取")
    net.add_argument("--offline", action="store_true",
                     help="只用缓存，不发起网络请求")
    net.add_argument("--cache-ttl", type=float, default=None, metavar="SECONDS",
                     help="缓存有效期；超过则重新抓取（默认永久有效）")
    net.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                     help=f"单次请求超时（默认 {DEFAULT_TIMEOUT}）")
    net.add_argument("--retries", type=int, default=DEFAULT_RETRIES, metavar="N",
                     help=f"单个请求失败后的重试次数（默认 {DEFAULT_RETRIES}）")
    net.add_argument("--interval", type=float, default=0.35, metavar="SECONDS",
                     help="请求之间的最小间隔，调大可以显著降低触发人机校验的概率"
                          "（默认 0.35）")
    net.add_argument("--max-notes", type=int, default=20000, metavar="N",
                     help="单篇论文最多抓取的 note 数（默认 20000）")

    misc = parser.add_argument_group("其他")
    misc.add_argument("-q", "--quiet", action="store_true", help="只输出结果路径")
    misc.add_argument("--on-error", choices=("warn", "raise"), default="warn",
                      help="批量模式下一篇失败时的行为（默认继续）")
    misc.add_argument("--tree", action="store_true",
                      help="在终端打印讨论串的树状结构")
    misc.add_argument("-V", "--version", action="version", version="openreview-crawler 1.0.0")
    return parser


def collect_queries(args: argparse.Namespace) -> list[str]:
    queries: list[str] = list(args.inputs)
    for path in args.input_file:
        file_path = Path(path)
        if not file_path.exists():
            raise SystemExit(f"找不到输入文件: {path}")
        # utf-8-sig: Notepad and PowerShell's Set-Content write a BOM, which
        # would otherwise turn the first "# comment" line into a search query.
        for line in file_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().lstrip("\ufeff").strip()
            if not line or line.startswith("#"):
                continue
            queries.append(line)
    # De-duplicate while keeping the user's order.
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        key = query.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(query.strip())
    return unique


def slugify(text: str, limit: int = 50) -> str:
    """ASCII-ish slug, kept for callers that want a URL-safe name."""
    keep = [c if c.isalnum() else "-" for c in text.lower()]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:limit].strip("-") or "paper"


def _display_width(text: str) -> int:
    """Terminal cells a string occupies.

    CJK characters are double-width, so padding by ``len()`` misaligns every
    column in a Chinese-language table.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: object, width: int, right: bool = False) -> str:
    """Pad (or truncate) to an exact display width."""
    text = "" if text is None else str(text)
    if _display_width(text) > width:
        kept, used = "", 0
        for char in text:
            char_width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
            if used + char_width > width - 1:
                break
            kept += char
            used += char_width
        text = kept + "…"
    padding = " " * max(0, width - _display_width(text))
    return padding + text if right else text + padding


def print_summary(paper: Paper) -> None:
    print()
    print(BAR)
    print(paper.title)
    if paper.venue:
        print(f"{paper.venue}  ·  {paper.forum_url}")
    else:
        print(paper.forum_url)
    mean = paper.mean_rating
    bits = [f"{len(paper.reviews)} 位审稿人"]
    if mean is not None:
        bits.append(f"平均评分 {mean:.2f}（{paper.rating_spread}）")
    if paper.author_responses:
        bits.append(f"{len(paper.author_responses)} 条作者回复")
    if paper.comments:
        bits.append(f"{len(paper.comments)} 条讨论")
    print("  ·  ".join(bits))
    if paper.decision_text:
        print(f"  最终决定: {paper.decision_text}")
    print()

    rows = paper.reviewer_rows()
    if rows:
        columns = [
            ("审稿人", 30, False), ("评分", 7, True), ("置信度", 8, True),
            ("问题", 5, True), ("后续", 5, True), ("作者回复", 9, True),
        ]
        time_width = 16  # "2023-11-06 08:00"
        header = "".join(_pad(name, width, right) for name, width, right in columns)
        header += "  " + _pad("提交时间", time_width)
        print(header)
        print("-" * _display_width(header))
        for row in rows:
            cells = [
                _pad(row["reviewer"], 30),
                _pad(str(row["rating"]).split(" ")[0], 7, right=True),
                _pad(str(row["confidence"]).split(" ")[0], 8, right=True),
                _pad(row["questions"], 5, right=True),
                _pad(row["followups"], 5, right=True),
                _pad(row["author_replied"], 9, right=True),
            ]
            print("".join(cells) + "  " + _pad(row["posted"], time_width))
        if mean is not None:
            print(f"\n  平均评分 {mean:.2f}  ·  {paper.rating_spread}")
    for warning in paper.warnings:
        print(f"  ! {warning}")


def print_tree(paper: Paper) -> None:
    if paper.submission is None:
        return
    print()
    print("讨论串结构:")

    def walk(node, depth: int = 0) -> None:
        indent = "  " * depth
        mark = {"author_response": "[作者]", "comment": "[讨论]", "review": "[审稿]",
                "meta_review": "[元审稿]", "decision": "[决定]"}.get(node.kind, "[其他]")
        label = node.label or node.role or node.kind
        print(f"{indent}{mark} {label}  ({node.created_str})")
        for child in node.children:
            walk(child, depth + 1)

    walk(paper.submission)


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    # -- authentication ---------------------------------------------------- #
    if args.logout:
        removed = delete_token_file(args.token_file)
        print("已删除本地令牌。" if removed else "本地没有保存令牌。")
        if not args.login:
            return 0

    if args.login:
        try:
            interactive_login(API2, email=args.email, token_path=args.token_file)
        except AuthError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        except (EOFError, KeyboardInterrupt):
            print("\n已取消登录。", file=sys.stderr)
            return 130
        if not args.inputs and not args.input_file:
            return 0

    queries = collect_queries(args)
    if not queries:
        parser.print_help()
        print("\n错误：请至少给出一篇论文（链接 / 标题 / id），或用 --input-file 指定列表文件。")
        return 2

    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    unknown = formats - {"md", "html", "markdown"}
    if unknown:
        parser.error(f"未知的输出格式: {', '.join(sorted(unknown))}（支持 md, html）")
    if "markdown" in formats:
        formats.add("md")

    verbose = not args.quiet and not args.stdout
    log = print if verbose else (lambda *_: None)

    token = resolve_token(args.token, args.token_file)

    # The token lasts exactly 24 hours and cannot be refreshed without a browser
    # cookie, so the expiry check happens here: sign in now if needed, rather
    # than failing halfway through a batch.
    if not args.offline and not args.no_prompt and args.token is None:
        token = ensure_token(
            API2,
            token_path=args.token_file,
            token=token,
            email=args.email,
            prompter=console_prompter,
            log=log,
        )
        if token is None:
            print("没有登录，已取消。", file=sys.stderr)
            return 1
    elif token:
        log(f"令牌状态：{token_status(token)}")

    client = OpenReviewClient(
        token=token,
        cookie=resolve_cookie(args.cookie),
        cache_dir=None if args.no_cache else args.cache_dir,
        cache_ttl=args.cache_ttl,
        offline=args.offline,
        refresh=args.refresh,
        timeout=args.timeout,
        retries=args.retries,
        min_interval=args.interval,
        verbose=False,
    )
    if verbose and token:
        claims = jwt_claims(token)
        who = claims.get("user", {}).get("id") if isinstance(claims.get("user"), dict) else ""
        print(f"访问方式：已登录{('（' + str(who) + '）') if who else ''}")
    elif verbose and not token:
        print("访问方式：匿名（如果频繁触发人机校验，请先登录）")

    # -- candidate listing only ------------------------------------------- #
    if args.list_candidates:
        if len(queries) != 1:
            parser.error("--list-candidates 只接受一篇论文作为输入")
        candidates: list[Candidate] = search_papers(client, queries[0], args.venue)
        if not candidates:
            print("没有找到任何候选。")
            return 1
        print(f"\n为 {queries[0]!r} 找到 {len(candidates)} 个候选：\n")
        for i, candidate in enumerate(candidates):
            print(f"  [{i}] {candidate}")
        print("\n用 --pick N 选定其中一个，或直接用它的链接。")
        return 0

    # -- crawl ------------------------------------------------------------- #
    if verbose:
        print(f"开始抓取 {len(queries)} 篇论文"
              f"{'（匿名访问）' if not client.token else '（使用 token）'} …")

    # A token can be rejected before its exp claim (signed out elsewhere). When
    # that happens mid-batch, sign in and run the whole thing again rather than
    # reporting every remaining paper as broken.
    for attempt in range(2):
        try:
            papers, failures = crawl_many(
                client, queries, on_error=args.on_error, log=log,
                venue_hint=args.venue, pick=args.pick,
                min_score=args.min_score, interactive=args.interactive,
            )
            break
        except TokenExpired as exc:
            if attempt or args.no_prompt or args.offline:
                print(f"\n{exc}", file=sys.stderr)
                return 1
            print(f"\n{str(exc).splitlines()[0]}", file=sys.stderr)
            renewed = ensure_token(
                API2,
                token_path=args.token_file,
                token=client.token,
                email=args.email,
                prompter=console_prompter,
                log=log,
                force=True,
            )
            if renewed is None:
                print("没有重新登录，已取消。", file=sys.stderr)
                return 1
            client.token = renewed
            if verbose:
                print("已重新登录，继续抓取 …")
    else:  # pragma: no cover - loop always breaks or returns
        return 1

    if not papers:
        print("\n没有任何论文抓取成功。", file=sys.stderr)
        for query, error in failures:
            print(f"  ✗ {query}\n    {error}", file=sys.stderr)
        return 1

    if args.stdout:
        for paper in papers:
            sys.stdout.write(render_paper(paper, include_abstract=not args.no_abstract))
            sys.stdout.write("\n")
        return 0

    # -- write output ------------------------------------------------------ #
    out_dir = Path(args.out)
    written = write_reports(
        papers,
        out_dir,
        formats=formats,
        include_abstract=not args.no_abstract,
        flat=args.flat,
        log=log,
    )

    for paper in papers:
        print_summary(paper)
    if args.tree:
        for paper in papers:
            print_tree(paper)

    print()
    print(BAR)
    print(f"已生成 {len(written)} 个文件：")
    for item in written:
        size = item.path.stat().st_size
        print(f"  {item.display(out_dir):<58} {size / 1024:>8.1f} KB")

    if "html" in formats:
        combined = out_dir / "report.html"
        if len(papers) > 1 and combined.exists():
            print(f"\n建议先打开汇总报告：{combined.resolve()}")
        else:
            first = next((i for i in written
                          if i.kind == "html" and i.path.parent == out_dir), None)
            if first is None:
                first = next((i for i in written if i.kind == "html"), None)
            if first is not None:
                print(f"\n建议先打开：{first.path.resolve()}")

    if failures:
        print(f"\n有 {len(failures)} 篇失败：", file=sys.stderr)
        for query, error in failures:
            print(f"  ✗ {query}\n    {error}", file=sys.stderr)
        return 1 if not papers else 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
