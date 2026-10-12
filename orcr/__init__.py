"""openreview-crawler: pull reviewers, reviews, questions and author replies
from OpenReview into a readable report.

Quick start::

    python crawl.py "https://openreview.net/forum?id=XXXX"
    python crawl.py "Some Paper Title" --venue ICLR
    python crawl.py --input-file papers.txt --out ./output
"""

from .api import API2, OpenReviewClient, OpenReviewError, NotFound, unwrap_content
from .extract import Message, Paper, build_paper, clean_text, parse_score
from .pipeline import crawl, crawl_many, fetch_thread
from .qa import QAItem, QASet, build_qa
from .resolve import Candidate, ResolveError, resolve, search_papers, title_similarity

__version__ = "1.0.0"

__all__ = [
    "API2",
    "OpenReviewClient",
    "OpenReviewError",
    "NotFound",
    "Paper",
    "Message",
    "build_paper",
    "clean_text",
    "parse_score",
    "unwrap_content",
    "crawl",
    "crawl_many",
    "fetch_thread",
    "build_qa",
    "QAItem",
    "QASet",
    "resolve",
    "search_papers",
    "title_similarity",
    "Candidate",
    "ResolveError",
    "__version__",
]
