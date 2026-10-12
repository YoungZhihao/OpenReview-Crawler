"""Build the double-clickable .exe launchers.

Uses the C# compiler that ships with Windows (``csc.exe``), so nothing has to
be downloaded or installed. Two executables are produced from one source file:

    OpenReview爬虫.exe   the full window: sign in + crawl + open the report
    OpenReview登录.exe   just the credential prompt, for refreshing the token

Run it directly, or double-click ``build_exe.bat``:

    python tools/build_exe.py
    python tools/build_exe.py --standalone    # also try a PyInstaller bundle
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orcr.console import fix_console  # noqa: E402

fix_console()

LAUNCHER = ROOT / "tools" / "launcher.cs"

# csc lives in the 32- or 64-bit .NET Framework directory depending on the box.
CSC_CANDIDATES = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64"
    / "v4.0.30319" / "csc.exe",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework"
    / "v4.0.30319" / "csc.exe",
]

TARGETS = [
    ("OpenReview爬虫.exe", [], "tools/app_gui.py"),
    ("OpenReview登录.exe", ["/define:LOGIN"], "tools/app_gui.py"),
    ("OpenReview清理.exe", ["/define:CLEANUP"], "tools/cleanup_gui.py"),
]


def find_csc() -> Path | None:
    for candidate in CSC_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def build_with_csc(csc: Path) -> int:
    print(f"编译器: {csc}")
    failures = 0
    for name, extra, script in TARGETS:
        if not (ROOT / script).exists():
            print(f"  ✗ {name} 跳过：找不到 {script}")
            failures += 1
            continue
        output = ROOT / name
        command = [
            str(csc), "/nologo", "/target:winexe", "/platform:anycpu",
            "/optimize+", f"/out:{output}", *extra, str(LAUNCHER),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not output.exists():
            print(f"  ✗ {name} 编译失败")
            print((result.stdout or "") + (result.stderr or ""))
            failures += 1
            continue
        size = output.stat().st_size / 1024
        print(f"  ✓ {name}  ({size:.1f} KB)  →  {script}")
    return failures


def build_standalone() -> int:
    """Optional: a bundle that does not need the user's Python."""
    if shutil.which("pyinstaller") is None and not _module_available("PyInstaller"):
        print("\n未安装 PyInstaller，跳过独立打包（不影响上面生成的 exe）。")
        print("如需完全脱离 Python 的单文件版本，先运行：")
        print("    pip install pyinstaller")
        return 0

    print("\n用 PyInstaller 打包独立版本（可能需要一两分钟）…")
    entry = ROOT / "tools" / "app_gui.py"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--name", "OpenReview爬虫",
        "--paths", str(ROOT),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(entry),
    ]
    result = subprocess.run(command)
    if result.returncode != 0:
        print("  ✗ PyInstaller 打包失败（上面的 exe 仍可正常使用）")
        return 1
    print(f"  ✓ {ROOT / 'dist' / 'OpenReview爬虫' / 'OpenReview爬虫.exe'}")
    return 0


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.name != "nt":
        print("这些启动器是为 Windows 生成的（需要 csc.exe）。")
        return 1
    if not LAUNCHER.exists():
        print(f"找不到 {LAUNCHER}")
        return 1

    csc = find_csc()
    if csc is None:
        print("没有找到 csc.exe（.NET Framework 自带的 C# 编译器）。")
        print("它在绝大多数 Windows 上位于：")
        for candidate in CSC_CANDIDATES:
            print(f"  {candidate}")
        print("\n可以先直接用 login.bat / crawl.bat 启动，功能完全一样。")
        return 1

    failures = build_with_csc(csc)
    if "--standalone" in argv:
        failures += build_standalone()
    elif failures == 0:
        print("\n提示：加 --standalone 可以额外尝试打包成不依赖 Python 的单文件版本。")

    print()
    if failures:
        print(f"有 {failures} 个目标失败。")
        return 1
    print("完成。双击这些 exe 即可使用：")
    for name, _, script in TARGETS:
        print(f"  {name:<22} {script}")
    print("\n如果双击后提示找不到 Python，可在本目录新建 python-path.txt，")
    print("在里面写一行 pythonw.exe 的完整路径。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
