"""A small Tkinter front end: sign in, then crawl.

Why this exists
---------------
The command line already does everything, but two things are easier with a
window: typing the password (masked, and asked for only when the 24-hour token
has actually run out) and pasting a paper without remembering flags.

This module deliberately contains no crawling logic of its own. It drives the
same :mod:`orcr` package the CLI uses, so there is exactly one implementation
of fetching, parsing and rendering.

Run it directly, or through the generated ``OpenReview爬虫.exe``:

    python tools/app_gui.py
    python tools/app_gui.py --login-only
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

from orcr.api import API2, OpenReviewClient, OpenReviewError, TokenExpired  # noqa: E402
from orcr.auth import (  # noqa: E402
    DEFAULT_TOKEN_FILE,
    AuthError,
    delete_token_file,
    ensure_token,
    load_token_file,
    remembered_email,
    token_is_expired,
    token_status,
)
from orcr.console import fix_console  # noqa: E402
from orcr.gui import (  # noqa: E402
    content_fits,
    enable_dpi_awareness,
    measurable,
    reveal_fitted,
)
from orcr.output import DEFAULT_OUT_DIR, write_reports  # noqa: E402
from orcr.pipeline import crawl_many  # noqa: E402

fix_console()

BG = "#f5f6f8"
ACCENT = "#a3161a"


# --------------------------------------------------------------------------- #
# modal login dialog
# --------------------------------------------------------------------------- #
class LoginDialog(tk.Toplevel):
    """Ask for an email and a masked password.

    Built by hand rather than with :mod:`tkinter.simpledialog` because the
    password must be masked and because the caller wants a pre-filled email.
    """

    def __init__(self, parent: tk.Misc, email: str = "", reason: str = ""):
        super().__init__(parent)
        self.title("登录 OpenReview")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result: tuple[str, str] | None = None

        frame = tk.Frame(self, bg=BG, padx=22, pady=18)
        frame.pack(fill="both", expand=True)

        if reason:
            tk.Label(frame, text=reason, bg=BG, fg=ACCENT, wraplength=360,
                     justify="left", font=("Microsoft YaHei UI", 10, "bold")
                     ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        tk.Label(frame, text="邮箱", bg=BG, anchor="w").grid(
            row=1, column=0, sticky="w", pady=4)
        self.email_var = tk.StringVar(value=email)
        email_entry = ttk.Entry(frame, textvariable=self.email_var, width=38)
        email_entry.grid(row=1, column=1, sticky="we", pady=4)

        tk.Label(frame, text="密码", bg=BG, anchor="w").grid(
            row=2, column=0, sticky="w", pady=4)
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(frame, textvariable=self.password_var,
                                        show="●", width=38)
        self.password_entry.grid(row=2, column=1, sticky="we", pady=4)

        note = tk.Label(
            frame,
            text="密码只用于换取令牌，不会被保存，也不会写入任何文件。",
            bg=BG, fg="#666", wraplength=360, justify="left",
        )
        note.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 12))

        buttons = tk.Frame(frame, bg=BG)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=self._cancel).pack(
            side="right", padx=(8, 0))
        ttk.Button(buttons, text="登录", command=self._submit).pack(side="right")

        self.bind("<Return>", lambda _event: self._submit())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.update_idletasks()
        self._centre(parent)
        if not email:
            email_entry.focus_set()
        else:
            self.password_entry.focus_set()
        self.transient(parent)
        self.grab_set()

    def _centre(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 3)}")
        except tk.TclError:
            pass

    def _submit(self) -> None:
        email = self.email_var.get().strip()
        password = self.password_var.get()
        if not email or not password:
            messagebox.showwarning("信息不完整", "请填写邮箱和密码。", parent=self)
            return
        self.result = (email, password)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class CrawlerApp:
    def __init__(self, root: tk.Tk, login_only: bool = False,
                 visible: bool = True) -> None:
        self.root = root
        self.login_only = login_only
        self.visible = visible
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.last_report: Path | None = None
        self.out_dir = ROOT / DEFAULT_OUT_DIR
        self.formats: tuple[str, ...] = ("md", "html")

        root.title("OpenReview 审稿爬虫")
        root.configure(bg=BG)
        # Deliberately no geometry() here: the window is sized from its own
        # content at the end of __init__. A hard-coded pixel size is measured
        # in physical pixels, so on a high-DPI display it clips the lower
        # controls -- exactly what happened to the cleanup window.

        self._build_login()
        if not login_only:
            self._build_crawl()
        self._build_bottom()

        self._refresh_token_status()
        self.root.after(120, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Now that every widget exists, size to fit it. The window is hidden
        # during construction, so reveal_fitted() measures a real layout before
        # anything is shown.
        if login_only:
            self.window_size = reveal_fitted(root, min_size=(480, 0), margin=(0, 10),
                                             show=self.visible)
        else:
            self.window_size = reveal_fitted(root, min_size=(880, 620), margin=(0, 14),
                                             show=self.visible)

        if login_only:
            self.root.after(200, self.do_login)

    # -- layout ----------------------------------------------------------- #
    def _build_login(self) -> None:
        box = tk.LabelFrame(self.root, text=" 账号 ", bg=BG, padx=14, pady=10)
        box.pack(fill="x", padx=14, pady=(14, 8))

        row = tk.Frame(box, bg=BG)
        row.pack(fill="x")

        tk.Label(row, text="邮箱", bg=BG).grid(row=0, column=0, sticky="w", pady=3)
        self.email_var = tk.StringVar(value=remembered_email(DEFAULT_TOKEN_FILE))
        ttk.Entry(row, textvariable=self.email_var, width=34).grid(
            row=0, column=1, sticky="we", pady=3, padx=(8, 0))

        tk.Label(row, text="密码", bg=BG).grid(row=1, column=0, sticky="w", pady=3)
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(row, textvariable=self.password_var,
                                        show="●", width=34)
        self.password_entry.grid(row=1, column=1, sticky="we", pady=3, padx=(8, 0))
        self.password_entry.bind("<Return>", lambda _e: self.do_login())

        row.columnconfigure(1, weight=1)

        actions = tk.Frame(box, bg=BG)
        actions.pack(fill="x", pady=(8, 0))
        self.login_button = ttk.Button(actions, text="登录并保存令牌",
                                       command=self.do_login)
        self.login_button.pack(side="left")
        self.forget_button = ttk.Button(actions, text="清除令牌", command=self.do_logout)
        self.forget_button.pack(side="left", padx=8)

        self.status_var = tk.StringVar(value="…")
        self.status_label = tk.Label(box, textvariable=self.status_var, bg=BG,
                                     fg="#444", wraplength=560, justify="left")
        self.status_label.pack(anchor="w", pady=(8, 0))

    def _build_crawl(self) -> None:
        box = tk.LabelFrame(self.root, text=" 论文 ", bg=BG, padx=14, pady=10)
        box.pack(fill="x", padx=14, pady=8)

        tk.Label(box, text="链接 / 标题 / arXiv 号 / DOI（多篇可用逗号或换行分隔）",
                 bg=BG, fg="#555", anchor="w").pack(fill="x")

        # An explicit character width keeps the window's opening size
        # predictable: tk.Text defaults to 80 characters, so without this the
        # content-driven width would swing with the font metrics.
        self.query_text = tk.Text(box, height=3, width=52, wrap="word",
                                  font=("Consolas", 10))
        self.query_text.pack(fill="x", pady=(4, 8))

        actions = tk.Frame(box, bg=BG)
        actions.pack(fill="x")
        self.crawl_button = ttk.Button(actions, text="开始抓取", command=self.do_crawl)
        self.crawl_button.pack(side="left")
        ttk.Button(actions, text="从文件导入列表…",
                   command=self.do_import).pack(side="left", padx=8)

        self.out_var = tk.StringVar(value=str(self.out_dir))
        # wraplength matters here: an unconstrained label showing the full
        # output path asked for 710 px on its own and dictated the whole
        # window's width.
        tk.Label(actions, textvariable=self.out_var, bg=BG, fg="#777",
                 wraplength=300, justify="right").pack(side="right")

    def _build_bottom(self) -> None:
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True, padx=14, pady=(4, 12))

        if not self.login_only:
            tk.Label(container, text="日志", bg=BG, anchor="w").pack(fill="x")
            self.log_text = tk.Text(container, wrap="word", height=8, width=52,
                                    font=("Consolas", 9), bg="#fbfbfd")
            scroll = ttk.Scrollbar(container, command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
            scroll.pack(side="right", fill="y")
            self.log_text.pack(side="top", fill="both", expand=True)
        else:
            self.log_text = None

        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=14, pady=(0, 14))
        self.open_report_button = ttk.Button(bottom, text="打开报告",
                                             command=self.open_report, state="disabled")
        self.open_report_button.pack(side="left")
        ttk.Button(bottom, text="打开输出文件夹",
                   command=self.open_out_dir).pack(side="left", padx=8)
        ttk.Button(bottom, text="退出", command=self._on_close).pack(side="right")

    # -- logging ---------------------------------------------------------- #
    def log(self, message: str = "") -> None:
        self.log_queue.put(str(message))

    def _drain_log(self) -> None:
        wrote = False
        try:
            while True:
                line = self.log_queue.get_nowait()
                if self.log_text is not None:
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", line + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                else:
                    print(line, flush=True)
                wrote = True
        except queue.Empty:
            pass
        if wrote and self.log_text is not None:
            self.log_text.see("end")
        self.root.after(120, self._drain_log)

    def _refresh_token_status(self) -> None:
        token = load_token_file(DEFAULT_TOKEN_FILE)
        status = token_status(token)
        if token is None:
            status += " —— 点「登录并保存令牌」即可"
        elif token_is_expired(token):
            status += " —— 下一次抓取时会提示你输入密码"
        self.status_var.set(status)

    # -- credential prompt (called from the worker thread) ---------------- #
    def _ask_credentials(self, default_email: str) -> tuple[str, str] | None:
        """Block the worker thread while the GUI asks for credentials.

        Called from the worker thread, so the dialog itself is created on the
        Tk thread via ``after`` and the worker waits on an event. The timeout
        is a safety net: a worker must never hang forever if the window dies.
        """
        holder: dict = {}
        done = threading.Event()

        def ask() -> None:
            try:
                dialog = LoginDialog(
                    self.root,
                    email=default_email or self.email_var.get().strip(),
                    reason="需要登录（令牌约 24 小时过期一次）",
                )
                self.root.wait_window(dialog)
                holder["result"] = dialog.result
            except Exception:  # noqa: BLE001 - never leave the worker hanging
                holder["result"] = None
            finally:
                done.set()

        self.root.after(0, ask)
        if not done.wait(timeout=600):
            return None
        result = holder.get("result")
        if result:
            self.root.after(0, lambda: self.email_var.set(result[0]))
        return result

    # -- actions ---------------------------------------------------------- #
    def do_login(self) -> None:
        email = self.email_var.get().strip() or remembered_email(DEFAULT_TOKEN_FILE)
        password = self.password_var.get()
        if not password:
            messagebox.showinfo("需要密码", "请输入密码。", parent=self.root)
            self.password_entry.focus_set()
            return

        self.login_button.configure(state="disabled")
        self.log(f"正在以 {email} 登录 …")

        def work() -> None:
            try:
                token = ensure_token(
                    API2, token_path=DEFAULT_TOKEN_FILE, token=None,
                    email=email, force=True, log=self.log,
                    prompter=lambda _default: (email, password),
                )
                if token is None:
                    self.log("登录已取消。")
                else:
                    self.password_var.set("")
                self.log(token_status(token))
            except AuthError as exc:
                self.log(f"登录失败：{exc}")
                self.root.after(0, lambda: messagebox.showerror(
                    "登录失败", str(exc), parent=self.root))
            except Exception as exc:  # noqa: BLE001
                self.log(f"登录出错：{type(exc).__name__}: {exc}")
            finally:
                self.root.after(0, self._after_login)

        if self.login_only:
            # Keep the login-only window responsive and short-lived.
            threading.Thread(target=work, daemon=True).start()
        else:
            threading.Thread(target=work, daemon=True).start()

    def _after_login(self) -> None:
        self.login_button.configure(state="normal")
        self._refresh_token_status()

    def do_logout(self) -> None:
        if delete_token_file(DEFAULT_TOKEN_FILE):
            self.log("已清除本地令牌。")
        else:
            self.log("本地没有保存令牌。")
        self._refresh_token_status()

    def do_import(self) -> None:
        path = filedialog.askopenfilename(
            title="选择论文列表文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            messagebox.showerror("读不了文件", str(exc), parent=self.root)
            return
        queries = [line.strip() for line in lines
                   if line.strip() and not line.strip().startswith("#")]
        self.query_text.delete("1.0", "end")
        self.query_text.insert("1.0", "\n".join(queries))
        self.log(f"从 {Path(path).name} 导入 {len(queries)} 条。")

    def _collect_queries(self) -> list[str]:
        raw = self.query_text.get("1.0", "end")
        parts: list[str] = []
        for line in raw.splitlines():
            line = line.strip().lstrip("\ufeff").strip()
            if not line:
                continue
            parts.extend(p.strip() for p in line.split(",") if p.strip())
        seen: set[str] = set()
        out: list[str] = []
        for part in parts:
            if part.lower() not in seen:
                seen.add(part.lower())
                out.append(part)
        return out

    def do_crawl(self) -> None:
        if self.busy:
            return
        queries = self._collect_queries()
        if not queries:
            messagebox.showinfo("没有输入", "请先粘贴论文链接或标题。", parent=self.root)
            return

        self.busy = True
        self.crawl_button.configure(state="disabled")
        self.login_button.configure(state="disabled")
        self.worker = threading.Thread(
            target=self._crawl_worker, args=(queries,), daemon=True)
        self.worker.start()

    # -- the actual work -------------------------------------------------- #
    def _crawl_worker(self, queries: list[str]) -> None:
        try:
            self.log(f"开始抓取 {len(queries)} 篇 …")

            client = OpenReviewClient(
                cache_dir=str(ROOT / ".cache"),
                min_interval=0.35,
            )

            # Sign in up front when the token is missing or stale. Asking here
            # rather than after a failure matters: an anonymous request is
            # answered with the CAPTCHA challenge, not a token error, so a
            # retry-on-401 would never fire.
            token = ensure_token(
                API2,
                token_path=DEFAULT_TOKEN_FILE,
                token=load_token_file(DEFAULT_TOKEN_FILE),
                email=self.email_var.get().strip(),
                prompter=self._ask_credentials,
                log=self.log,
            )
            if token is None:
                self.log("没有登录，已取消。")
                return
            client.token = token

            papers, failures = self._crawl_with_retry(client, queries)
            if papers is None:
                return

            if not papers:
                self.log("没有抓到任何论文。")
                for query, error in failures:
                    self.log(f"  ✗ {query}: {error.splitlines()[0]}")
                return

            written = self._write(papers)
            self.log(f"完成：{len(papers)} 篇，{len(written)} 个文件")
            for item in written:
                self.log(f"  {item.display(self.out_dir)}")
            if failures:
                self.log(f"有 {len(failures)} 篇失败：")
                for query, error in failures:
                    self.log(f"  ✗ {query}: {error.splitlines()[0]}")

            if len(papers) > 1 and "html" in self.formats:
                target = self.out_dir / "report.html"
            else:
                target = next((i.path for i in written if i.kind == "html"), None)
            if target is not None and target.exists():
                self.last_report = target
                self.root.after(0, lambda: self.open_report_button.configure(
                    state="normal"))
                self.root.after(0, lambda t=target: self._offer_open(t))
        except AuthError as exc:
            self.log(f"登录失败：{exc}")
        except OpenReviewError as exc:
            self.log(f"接口出错：{str(exc).splitlines()[0]}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"意外错误：{type(exc).__name__}: {exc}")
            self.log(traceback.format_exc())
        finally:
            self.root.after(0, self._after_crawl)

    def _crawl_with_retry(self, client: OpenReviewClient, queries: list[str]):
        """Run the batch, signing in again once if the token is rejected."""
        for attempt in range(2):
            try:
                return crawl_many(client, queries, on_error="warn", log=self.log)
            except TokenExpired:
                if attempt:
                    raise
                self.log("令牌已失效，请重新登录 …")
                renewed = ensure_token(
                    API2, token_path=DEFAULT_TOKEN_FILE, token=client.token,
                    email=self.email_var.get().strip(), force=True,
                    log=self.log, prompter=self._ask_credentials)
                if renewed is None:
                    self.log("没有重新登录，已取消。")
                    return None, []
                client.token = renewed
                self.log("已重新登录，继续抓取 …")
        return None, []

    def _write(self, papers) -> list:
        """Write reports using the shared layout (one folder per paper)."""
        return write_reports(
            papers,
            self.out_dir,
            formats=self.formats,
            log=self.log,
        )

    def _after_crawl(self) -> None:
        self.busy = False
        self.crawl_button.configure(state="normal")
        self.login_button.configure(state="normal")
        self._refresh_token_status()

    def _offer_open(self, target: Path) -> None:
        if messagebox.askyesno("完成", f"报告已生成：\n{target}\n\n现在打开吗？",
                               parent=self.root):
            self.open_report()

    def open_report(self) -> None:
        target = self.last_report
        if target is None or not target.exists():
            # Fall back to the newest report on disk, preferring the batch one.
            candidates: list[Path] = []
            if self.out_dir.exists():
                combined = self.out_dir / "report.html"
                if combined.exists():
                    candidates.append(combined)
                candidates.extend(sorted(self.out_dir.rglob("*.html"),
                                         key=lambda p: p.stat().st_mtime,
                                         reverse=True))
            target = next((c for c in candidates if c.name != "report.html"),
                          candidates[0] if candidates else None)
        if target is None or not target.exists():
            messagebox.showinfo("还没有报告", "先抓取一篇论文。", parent=self.root)
            return
        self.last_report = target
        webbrowser.open(target.as_uri())

    def open_out_dir(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(self.out_dir)  # noqa: S606 - Windows-only convenience
        else:
            webbrowser.open(self.out_dir.as_uri())

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
                "正在抓取", "抓取还没结束，确定要退出吗？", parent=self.root):
            return
        self.root.destroy()


def self_test() -> int:
    """Build both window layouts without showing them, and check the tree.

    This is what makes the generated .exe testable from a script: the launcher
    passes ``--self-test`` through and returns this exit code, so the whole
    chain (exe -> Python -> GUI construction) can be verified without flashing
    a window on someone's screen.

    Both layouts are built under a single Tk root, with the login-only window
    as a child toplevel. Creating and destroying several roots in one process
    makes Tcl print "application has been destroyed" from its theme handler,
    which looks like a failure but is not.
    """
    problems: list[str] = []
    root = tk.Tk()
    root.withdraw()

    def alive(widget) -> bool:
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    try:
        app = CrawlerApp(root, login_only=False, visible=False)
        root.update_idletasks()
        if root.state() != "withdrawn":
            problems.append("自检不应把窗口显示出来")

        checks = {
            "登录按钮": alive(app.login_button),
            "清除令牌按钮": alive(app.forget_button),
            "抓取按钮": alive(app.crawl_button),
            "打开报告按钮": alive(app.open_report_button),
            "密码输入框": alive(app.password_entry),
            "密码已遮蔽": app.password_entry.cget("show") == "●",
            "论文输入区": alive(app.query_text),
            "日志区": app.log_text is not None and alive(app.log_text),
        }
        for name, ok in checks.items():
            if not ok:
                problems.append(f"缺少控件或状态不对：{name}")

        # No control may sit outside the window at its opening size -- the
        # failure mode that made the cleanup window need manual resizing.
        with measurable(root):
            problems.extend(
                f"控件被裁到窗口外：{o}" for o in content_fits(root, [
                    ("登录按钮", app.login_button),
                    ("清除令牌按钮", app.forget_button),
                    ("抓取按钮", app.crawl_button),
                    ("打开报告按钮", app.open_report_button),
                    ("打开输出文件夹按钮", app.open_out_dir),
                    ("论文输入区", app.query_text),
                    ("日志区", app.log_text),
                ])
            )

        # The credential dialog must construct cleanly too.
        try:
            dialog = LoginDialog(root, email="someone@example.com", reason="自检")
            dialog.withdraw()
            if dialog.password_entry.cget("show") != "●":
                problems.append("登录对话框的密码框没有遮蔽")
            dialog.destroy()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"登录对话框构建失败：{type(exc).__name__}: {exc}")

        # And the login-only layout, as its own child window.
        try:
            child = tk.Toplevel(root)
            child.withdraw()
            app2 = CrawlerApp(child, login_only=True, visible=False)
            root.update_idletasks()
            if not app2.login_button.winfo_exists():
                problems.append("仅登录模式缺少登录按钮")
            child.destroy()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"仅登录模式构建失败：{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"主窗口构建失败：{type(exc).__name__}: {exc}")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

    if problems:
        print("self-test 失败：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("self-test 通过：主窗口与登录窗口均构建正常")
    print(f"  令牌文件：{DEFAULT_TOKEN_FILE}")
    print(f"  当前状态：{token_status(load_token_file(DEFAULT_TOKEN_FILE))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()

    login_only = "--login-only" in argv

    enable_dpi_awareness()
    root = tk.Tk()
    # Hidden during construction so reveal_fitted() can measure a real layout
    # without showing a mis-sized window first.
    root.withdraw()
    CrawlerApp(root, login_only=login_only)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
