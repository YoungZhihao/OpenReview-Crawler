"""A small window that deletes crawl results and intermediate files.

Double-click ``OpenReview清理.exe`` (or ``clean.bat``) and you get a list of
what can be removed, with sizes, each item toggled by a checkbox. The login
token is listed but **unchecked by default**, because deleting it means typing
the password again.

What it cannot do is delete the crawler. Every candidate path goes through
:func:`orcr.cleanup.is_deletable`, which allows only ``results/``, ``.cache/``,
``__pycache__`` and (if ticked) the token file -- and refuses to run at all
unless the folder really is the crawler's root.

    python tools/cleanup_gui.py              interactive window
    python tools/cleanup_gui.py --yes        delete the default selection, no UI
    python tools/cleanup_gui.py --self-test  build the window, check it, exit
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402
from tkinter import messagebox, ttk  # noqa: E402

from orcr.cleanup import (  # noqa: E402
    CleanupRefused,
    clean_all,
    find_project_root,
    format_size,
    remove,
    survey,
)
from orcr.console import fix_console  # noqa: E402
from orcr.gui import (  # noqa: E402
    content_fits,
    enable_dpi_awareness,
    measurable,
    reveal_fitted,
)

fix_console()

BG = "#f5f6f8"
ACCENT = "#a3161a"

# Files that must survive a cleanup, shown in the window so the promise is
# visible rather than buried in the code.
KEEP_NOTE = (
    "不会删除：crawl.py、orcr/、tools/、各 .bat、各 .exe、README.md  —— "
    "清理工具只被允许动 results/、.cache/、__pycache__ 和（可选）登录令牌。"
)


class CleanupApp:
    def __init__(self, root: tk.Tk, project_root: Path,
                 visible: bool = True) -> None:
        self.root = root
        self.project_root = project_root
        self.visible = visible
        self.targets = survey(project_root)
        self.vars: dict[str, tk.BooleanVar] = {}

        root.title("OpenReview 清理工具")
        root.configure(bg=BG)
        # No geometry() here on purpose: the window is sized from its content
        # once every widget exists (see the fit_window call at the end of
        # __init__). A hard-coded pixel size clips the lower controls on a
        # high-DPI display.

        header = tk.Frame(root, bg=BG, padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="删除抓取结果与中间文件", bg=BG,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        tk.Label(header, text=str(project_root), bg=BG, fg="#666",
                 wraplength=560, justify="left").pack(anchor="w", pady=(4, 0))

        listbox = tk.LabelFrame(root, text=" 可清理的内容 ", bg=BG,
                                padx=14, pady=10)
        listbox.pack(fill="both", expand=True, padx=18, pady=(0, 8))

        self.rows = tk.Frame(listbox, bg=BG)
        self.rows.pack(fill="both", expand=True)
        # Kept so --self-test can assert these are all inside the window.
        self.row_widgets: list[tuple[str, tk.Widget]] = []

        any_present = False
        for target in self.targets:
            variable = tk.BooleanVar(value=target.checked)
            self.vars[target.key] = variable
            row = tk.Frame(self.rows, bg=BG)
            row.pack(fill="x", pady=3)

            present = target.present
            any_present = any_present or present
            box = ttk.Checkbutton(row, variable=variable, text="")
            box.pack(side="left")
            if not present:
                box.state(["disabled"])

            name = target.label
            detail = target.description
            if present:
                detail += f"  ·  {target.file_count()} 个文件，{format_size(target.size())}"
            else:
                detail += "  ·  当前不存在"

            text = tk.Frame(row, bg=BG)
            text.pack(side="left", fill="x", expand=True, padx=(4, 0))
            tk.Label(text, text=name, bg=BG, anchor="w",
                     font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
            tk.Label(text, text=detail, bg=BG, fg="#666", anchor="w",
                     wraplength=500, justify="left").pack(anchor="w")
            self.row_widgets.append((target.label, row))

        self.keep_note = tk.Label(listbox, text=KEEP_NOTE, bg=BG, fg="#3d6b3d",
                                  wraplength=560, justify="left")
        self.keep_note.pack(anchor="w", pady=(10, 0))

        if not any_present:
            tk.Label(listbox, text="目前没有可清理的内容。", bg=BG, fg="#666"
                     ).pack(anchor="w", pady=(6, 0))

        self.status = tk.StringVar(value="")
        self.status_label = tk.Label(root, textvariable=self.status, bg=BG,
                                     fg="#444", wraplength=560, justify="left")
        self.status_label.pack(anchor="w", padx=18)

        buttons = tk.Frame(root, bg=BG, padx=18, pady=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="全选", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(buttons, text="全不选", command=lambda: self._set_all(False)).pack(side="left", padx=6)
        ttk.Button(buttons, text="退出", command=root.destroy).pack(side="right")
        self.delete_button = ttk.Button(buttons, text="删除所选", command=self.do_delete)
        self.delete_button.pack(side="right", padx=(0, 8))

        root.protocol("WM_DELETE_WINDOW", root.destroy)

        # Size to the content now that everything above exists. The window is
        # hidden while under construction, so reveal_fitted() can take a real
        # measurement before anything is shown.
        self.size = reveal_fitted(root, min_size=(600, 0), margin=(0, 12),
                                  show=self.visible)

    def _set_all(self, value: bool) -> None:
        for target in self.targets:
            if target.present:
                self.vars[target.key].set(value)

    def selected(self):
        return [t for t in self.targets
                if self.vars[t.key].get() and t.present]

    def do_delete(self) -> None:
        chosen = self.selected()
        if not chosen:
            messagebox.showinfo("没有选中", "请先勾选要删除的内容。", parent=self.root)
            return

        total = sum(t.size() for t in chosen)
        lines = "\n".join(
            f"  · {t.label}（{t.file_count()} 个文件，{format_size(t.size())}）"
            for t in chosen
        )
        if not messagebox.askyesno(
            "确认删除",
            f"将删除以下内容，且无法撤销：\n\n{lines}\n\n"
            f"合计 {format_size(total)}\n\n确定继续吗？",
            parent=self.root, default="no",
        ):
            self.status.set("已取消，没有删除任何内容。")
            return

        self.delete_button.configure(state="disabled")
        try:
            result = remove(chosen, self.project_root, log=self._note)
        except CleanupRefused as exc:
            messagebox.showerror("出于安全考虑已中止", str(exc), parent=self.root)
            self.delete_button.configure(state="normal")
            return

        self.status.set(result.summary)
        messagebox.showinfo(
            "清理完成",
            f"{result.summary}\n\n爬虫本体（crawl.py、orcr/、tools/、exe、bat）"
            "没有被改动。",
            parent=self.root,
        )
        self._refresh()

    def _note(self, message: str) -> None:
        self.status.set(message)

    def _refresh(self) -> None:
        # Rebuild the list so sizes reflect reality after a deletion.
        for child in self.rows.winfo_children():
            child.destroy()
        self.targets = survey(self.project_root)
        self.row_widgets = []
        for target in self.targets:
            variable = self.vars.setdefault(target.key, tk.BooleanVar(value=target.checked))
            present = target.present
            row = tk.Frame(self.rows, bg=BG)
            row.pack(fill="x", pady=3)
            box = ttk.Checkbutton(row, variable=variable, text="")
            box.pack(side="left")
            if not present:
                box.state(["disabled"])
            detail = target.description
            detail += (f"  ·  {target.file_count()} 个文件，{format_size(target.size())}"
                       if present else "  ·  当前不存在")
            text = tk.Frame(row, bg=BG)
            text.pack(side="left", fill="x", expand=True, padx=(4, 0))
            tk.Label(text, text=target.label, bg=BG, anchor="w",
                     font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
            tk.Label(text, text=detail, bg=BG, fg="#666", anchor="w",
                     wraplength=500, justify="left").pack(anchor="w")
            self.row_widgets.append((target.label, row))
        self.delete_button.configure(state="normal")


def self_test() -> int:
    """Build the window without showing it and check the safety rules."""
    problems: list[str] = []

    try:
        project_root = find_project_root()
    except CleanupRefused as exc:
        print(f"self-test 失败：{exc}")
        return 1

    # The guard must reject everything that is not crawl output.
    from orcr.cleanup import is_deletable

    must_survive = ["crawl.py", "orcr", "orcr/api.py", "tools", "tools/app_gui.py",
                    "README.md", "OpenReview爬虫.exe", "crawl.bat", ".gitignore"]
    for name in must_survive:
        if is_deletable(project_root / name, project_root):
            problems.append(f"安全检查有漏洞，竟然允许删除 {name}")

    must_go = ["results", ".cache", "results/report.html", "orcr/__pycache__"]
    for name in must_go:
        if not is_deletable(project_root / name, project_root):
            problems.append(f"应当允许删除却没有允许：{name}")

    # Nothing outside the project root is ever deletable.
    if is_deletable(Path("C:/Windows/System32"), project_root):
        problems.append("安全检查有漏洞，竟然允许删除项目外的路径")

    try:
        root = tk.Tk()
        root.withdraw()
        try:
            app = CleanupApp(root, project_root, visible=False)
            root.update_idletasks()
            if root.state() != "withdrawn":
                problems.append("自检不应把窗口显示出来")
            if not app.delete_button.winfo_exists():
                problems.append("窗口缺少删除按钮")
            if len(app.targets) < 4:
                problems.append("清理项少于预期")
            token = next((t for t in app.targets if t.key == "token"), None)
            if token is None:
                problems.append("缺少登录令牌清理项")
            elif token.checked:
                problems.append("登录令牌默认应为不勾选，避免误删")

            # Every control must be inside the window without resizing it.
            # This is the regression a hard-coded 620x470 geometry caused on a
            # 200%-scaled display: the bottom buttons fell outside the window.
            # measurable() maps the window at zero opacity so the layout is
            # real -- a withdrawn window reports child heights of 1.
            with measurable(root):
                offenders = content_fits(root, [
                    ("删除所选按钮", app.delete_button),
                    ("状态栏", app.status_label),
                    ("保留说明", app.keep_note),
                    ("选项列表区", app.rows),
                    *app.row_widgets,
                ])
            problems.extend(f"控件被裁到窗口外：{o}" for o in offenders)
        finally:
            root.destroy()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"窗口构建失败：{type(exc).__name__}: {exc}")

    if problems:
        print("self-test 失败：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("self-test 通过：清理窗口构建正常，安全检查覆盖 13 项断言")
    print(f"  项目根目录：{project_root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--self-test" in argv:
        return self_test()

    try:
        project_root = find_project_root()
    except CleanupRefused as exc:
        print(exc)
        if "--yes" not in argv:
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("无法确定项目目录", str(exc), parent=root)
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
        return 1

    if "--yes" in argv:
        include_token = "--include-token" in argv
        result = clean_all(project_root, include_token=include_token, log=print)
        print(result.summary)
        return 0

    enable_dpi_awareness()
    root = tk.Tk()
    # Hidden during construction so reveal_fitted() can measure a real layout
    # without showing a mis-sized window first.
    root.withdraw()
    CleanupApp(root, project_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
