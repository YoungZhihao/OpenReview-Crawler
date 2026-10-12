#!/usr/bin/env python3
"""Entry point for the OpenReview crawler.

    python crawl.py "https://openreview.net/forum?id=XXXX"
    python crawl.py "论文标题" --venue ICLR
    python crawl.py --input-file papers.txt

Requires nothing beyond the Python standard library (3.9+).
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):  # pragma: no cover
    sys.exit("需要 Python 3.9 或更高版本（当前 %d.%d）"
             % sys.version_info[:2])

from orcr.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
