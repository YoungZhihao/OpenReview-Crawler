"""Delete crawl output and intermediate files -- and nothing else.

This module exists to make one destructive operation safe. The rule it enforces
is an allowlist, not a denylist: a path may only be removed if its **first**
path component is one of :data:`ALLOWED_TOP`, or if it is a ``__pycache__``
directory. Everything else -- ``crawl.py``, ``orcr/``, ``tools/``, the ``.exe``
and ``.bat`` launchers, ``README.md`` -- is unreachable by construction, so no
argument or user mistake can delete the crawler itself.

On top of that: :func:`survey` refuses to run at all unless the directory really
looks like the crawler's root (it must contain ``crawl.py`` and an ``orcr/``
package). That guards the case where the tool is somehow launched from the
wrong folder and would otherwise offer to wipe an unrelated ``results/``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

# The only top-level entries cleanup is ever allowed to touch.
ALLOWED_TOP = {
    "results",
    ".cache",
    ".openreview_token",
    ".openreview_cookie",
}

# Belt and braces: even if a future edit widened the allowlist, these names are
# never removed. Kept as an explicit list so the intent is readable.
PROTECTED_NAMES = {
    "crawl.py", "crawl.bat", "login.bat", "build_exe.bat", "clean.bat",
    "README.md", ".gitignore", "orcr", "tools", "tests",
}
PROTECTED_SUFFIXES = {".exe", ".py", ".cs", ".bat", ".md"}


class CleanupRefused(RuntimeError):
    """Raised when the target directory is not a crawler root."""


@dataclass
class CleanTarget:
    key: str
    label: str
    paths: list[Path]
    description: str
    checked: bool = True

    @property
    def existing(self) -> list[Path]:
        return [p for p in self.paths if p.exists()]

    @property
    def present(self) -> bool:
        return bool(self.existing)

    def size(self) -> int:
        return sum(path_size(p) for p in self.existing)

    def file_count(self) -> int:
        total = 0
        for path in self.existing:
            if path.is_file():
                total += 1
            else:
                total += sum(1 for child in path.rglob("*") if child.is_file())
        return total


def path_size(path: Path) -> int:
    """Total bytes under a path; 0 for anything unreadable."""
    try:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
        return total
    except OSError:
        return 0


def is_crawler_root(path: Path) -> bool:
    """Whether ``path`` looks like the folder this tool belongs to."""
    try:
        return (path / "crawl.py").is_file() and (path / "orcr").is_dir()
    except OSError:
        return False


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from this file until the crawler root is found."""
    here = (start or Path(__file__).resolve().parent).resolve()
    for candidate in [here, *here.parents]:
        if is_crawler_root(candidate):
            return candidate
    raise CleanupRefused(
        "找不到爬虫根目录（需要同时存在 crawl.py 和 orcr/）。\n"
        f"从 {here} 向上都没有找到，为安全起见不做任何删除。"
    )


def is_deletable(path: Path, root: Path) -> bool:
    """The single guard every deletion goes through."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False  # outside the project root
    parts = relative.parts
    if not parts:
        return False  # the root itself
    if parts[-1] in PROTECTED_NAMES:
        return False
    if parts[0] in ALLOWED_TOP:
        return True
    if parts[-1] == "__pycache__":
        return True
    return False


def _pycache_dirs(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.rglob("__pycache__") if p.is_dir()
                      and is_deletable(p, root))
    except OSError:
        return []


def survey(root: Path) -> list[CleanTarget]:
    """Describe what could be deleted, with sizes, without deleting anything."""
    root = root.resolve()
    if not is_crawler_root(root):
        raise CleanupRefused(
            f"{root}\n不像爬虫根目录（缺少 crawl.py 或 orcr/），已中止。"
        )

    results_dir = root / "results"
    cache_dir = root / ".cache"

    return [
        CleanTarget(
            key="results",
            label="抓取结果报告",
            paths=[results_dir],
            description="results/ 里所有论文报告、汇总页与 paper.json",
            checked=True,
        ),
        CleanTarget(
            key="cache",
            label="接口响应缓存",
            paths=[cache_dir],
            description=".cache/ 里的原始 API 响应；删掉后下次抓取需重新联网",
            checked=True,
        ),
        CleanTarget(
            key="pycache",
            label="Python 字节码缓存",
            paths=_pycache_dirs(root),
            description="各处 __pycache__/，删掉无影响，会自动重建",
            checked=True,
        ),
        CleanTarget(
            key="token",
            label="登录令牌",
            paths=[root / ".openreview_token", root / ".openreview_cookie"],
            description="删除后需要重新输入一次密码（默认不勾选，避免误删）",
            checked=False,
        ),
    ]


@dataclass
class CleanResult:
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    freed: int = 0
    refused: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.removed and not self.refused:
            return "没有需要删除的内容。"
        parts = [f"已删除 {len(self.removed)} 项，释放 {format_size(self.freed)}"]
        if self.refused:
            parts.append(f"因安全检查跳过 {len(self.refused)} 项")
        return "，".join(parts) + "。"


def remove(targets: list[CleanTarget], root: Path, log=None) -> CleanResult:
    """Delete the selected targets. Refuses anything outside the allowlist."""
    root = root.resolve()
    if not is_crawler_root(root):
        raise CleanupRefused(
            f"{root}\n不像爬虫根目录（缺少 crawl.py 或 orcr/），已中止。"
        )

    result = CleanResult()
    for target in targets:
        for path in target.paths:
            if not path.exists():
                continue
            if not is_deletable(path, root):
                result.refused.append(str(path))
                if log:
                    log(f"  跳过（不在允许删除的范围内）：{path}")
                continue

            # Measure before deleting; afterwards there is nothing to measure.
            size = path_size(path)
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                result.skipped.append(str(path))
                if log:
                    log(f"  删除失败：{path}（{exc}）")
                continue

            result.removed.append(str(path))
            result.freed += size
            if log:
                log(f"  已删除：{path}")

    # Keep the results folder itself so the next run has a home.
    results_dir = root / "results"
    if not results_dir.exists() and any(t.key == "results" for t in targets):
        try:
            results_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    return result


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def clean_all(root: Path | None = None, include_token: bool = False,
              log=None) -> CleanResult:
    """Convenience wrapper: survey, select everything (bar the token), remove."""
    root = (root or find_project_root()).resolve()
    targets = [t for t in survey(root) if t.checked or (include_token and t.key == "token")]
    return remove(targets, root, log=log)
