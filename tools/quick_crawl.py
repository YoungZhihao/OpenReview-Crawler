"""Console-friendly wrapper used by crawl.bat.

Kept in Python rather than in the batch file so that the Chinese prompts are
printed by something that gets the console encoding right (PEP 528), instead of
by cmd.exe reading a GBK-vs-UTF-8 batch file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orcr.console import fix_console  # noqa: E402

fix_console()


def main() -> int:
    from orcr.cli import main as cli_main

    print()
    print("=" * 62)
    print(" OpenReview 审稿爬虫")
    print("=" * 62)
    print()
    print("粘贴论文的 OpenReview 链接、标题、arXiv 号或 DOI。")
    print("多篇用逗号分隔。直接回车则退出。")
    print()

    try:
        raw = input("论文 > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 130

    raw = raw.lstrip("\ufeff").strip()
    if not raw:
        print("没有输入，已退出。")
        return 0

    queries = [part.strip() for part in raw.replace("\n", ",").split(",")
               if part.strip()]
    if not queries:
        print("没有有效的论文条目，已退出。")
        return 0

    try:
        return cli_main([*queries, "--out", str(ROOT / "output")])
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
