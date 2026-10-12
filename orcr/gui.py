"""Tk layout helpers shared by the crawler window and the cleanup window.

Both bugs this module exists to prevent are the same bug in different clothes:
a window sized by a number someone guessed.

``geometry("620x470")`` is measured in **physical** pixels. Once the process is
DPI-aware -- which these windows are -- that same number is half the apparent
size on a 200%-scaled display, so the bottom row of controls ends up outside
the window and the user has to drag it larger before they can click anything.

The fix is to stop guessing: build the widgets first, then ask Tk what size the
content actually needs and use that. :func:`fit_window` also clamps to the
screen, because a content-driven height can otherwise exceed a short display.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import tkinter


def enable_dpi_awareness() -> None:
    """Tell Windows we handle scaling ourselves.

    Must run *before* the first window is created. Calling it afterwards leaves
    Tk and the window manager disagreeing about the scale factor, and the
    window comes out at roughly half the requested size.
    """
    if os.name != "nt":
        return
    try:
        from ctypes import windll

        # 1 = PROCESS_SYSTEM_DPI_AWARE (Windows 8.1+)
        windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        from ctypes import windll

        # Fallback for older systems.
        windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def fit_window(window: "tkinter.Misc", min_size: tuple[int, int] = (0, 0),
               margin: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Size a window to fit its contents, never clipping any control.

    Call this **after** every widget has been created, and preferably while the
    window is mapped -- see :func:`reveal_fitted` for why the latter matters.

    ``min_size`` only ever *grows* the window; the lower bound on height is the
    content's own requirement, so shrinking below "everything visible" is
    impossible rather than merely discouraged.
    """
    window.update_idletasks()

    needed_width = window.winfo_reqwidth()
    needed_height = window.winfo_reqheight()
    width = max(needed_width + margin[0], min_size[0])
    height = max(needed_height + margin[1], min_size[1])

    # Never open larger than the display, even if the content wants to be.
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    max_width = max(320, screen_width - 60)
    max_height = max(240, screen_height - 120)
    width = min(width, max_width)
    height = min(height, max_height)

    window.geometry(f"{width}x{height}")

    # The floor is what the content needs, capped by the screen.
    window.minsize(min(needed_width, max_width), min(needed_height, max_height))
    return width, height


def settle(window: "tkinter.Misc", rounds: int = 8) -> tuple[int, int]:
    """Run update cycles until the layout stops changing.

    Tk computes font metrics for the *unmapped* case at 96 DPI and only applies
    the display's real scaling once the window is actually mapped. On a 200%
    display that makes the first measurement after mapping come out at exactly
    half the true size -- so a single ``update()`` is not enough, and a window
    fitted from that number is half the height it needs to be.

    Waiting for the requisition to stop moving is cheap and removes the guess.
    """
    previous: tuple[int, int] | None = None
    for _ in range(rounds):
        window.update_idletasks()
        window.update()
        current = (window.winfo_reqwidth(), window.winfo_reqheight())
        if current == previous:
            return current
        previous = current
    return previous or (window.winfo_reqwidth(), window.winfo_reqheight())


def reveal_fitted(window: "tkinter.Misc", min_size: tuple[int, int] = (0, 0),
                  margin: tuple[int, int] = (0, 0), show: bool = True
                  ) -> tuple[int, int]:
    """Size a window to its content with a *real* layout pass, then show it.

    Measuring a hidden window is not reliable: text wrapping and font metrics
    only settle after the window is mapped and the display's DPI scaling has
    been applied. A window fitted from those early numbers comes out smaller
    than its own content -- which is precisely the bug being fixed here, and
    leaving it to the window manager to grow the window is luck, not design.

    So the window is mapped at zero opacity, laid out until the measurement
    stops moving, fitted, and only then made opaque. Nothing is visible until
    the size is already correct, so there is no flash of a mis-sized window.

    ``show=False`` keeps it hidden afterwards, which is what the ``--self-test``
    paths need: they must exercise the real layout without putting a window on
    someone's screen.
    """
    alpha_supported = True
    try:
        window.attributes("-alpha", 0.0)
    except Exception:  # noqa: BLE001 - not every platform supports alpha
        alpha_supported = False

    window.deiconify()
    settle(window)

    size = fit_window(window, min_size=min_size, margin=margin)

    if show:
        if alpha_supported:
            try:
                window.attributes("-alpha", 1.0)
            except Exception:  # noqa: BLE001
                pass
    else:
        window.withdraw()
        if alpha_supported:
            try:
                # Restore opacity so a later deiconify is not invisible.
                window.attributes("-alpha", 1.0)
            except Exception:  # noqa: BLE001
                pass
    return size


def content_fits(window: "tkinter.Misc", widgets) -> list[str]:
    """Names of widgets that are clipped, squeezed, or outside the window.

    Two distinct failure modes, both worth catching:

    * **outside the window** -- a control positioned past the right or bottom
      edge, which happens when the window is simply too small;
    * **squeezed below its requested size** -- Tk's pack manager shrinks an
      ``expand=True`` area when it runs out of room, so a too-short window
      silently clips the *contents* of that area while every control's edge
      still lies inside the window. This is the subtler and more likely case:
      it is why a cleanup list showed only three of its four options.

    Wrap the call in :func:`measurable`: on a withdrawn window every child
    reports a height of 1, and nothing can be verified from that.
    """
    window.update_idletasks()
    limit_x = window.winfo_rootx() + window.winfo_width()
    limit_y = window.winfo_rooty() + window.winfo_height()
    offenders: list[str] = []

    for name, widget in widgets:
        try:
            if not widget.winfo_exists() or not widget.winfo_ismapped():
                continue
            right = widget.winfo_rootx() + widget.winfo_width()
            bottom = widget.winfo_rooty() + widget.winfo_height()
            width, height = widget.winfo_width(), widget.winfo_height()
            wanted_width = widget.winfo_reqwidth()
            wanted_height = widget.winfo_reqheight()
        except Exception:  # noqa: BLE001 - a destroyed widget is not a failure
            continue

        if right > limit_x + 1 or bottom > limit_y + 1:
            offenders.append(f"{name} 超出窗口（右下角 {right},{bottom}，"
                             f"窗口边界 {limit_x},{limit_y}）")
        elif height + 1 < wanted_height or width + 1 < wanted_width:
            offenders.append(f"{name} 被压缩（实际 {width}x{height}，"
                             f"需要 {wanted_width}x{wanted_height}）")
    return offenders


@contextlib.contextmanager
def measurable(window: "tkinter.Misc"):
    """Make a hidden window measurable without showing it to anyone.

    A withdrawn window has not been laid out, so every child reports a height
    of 1 and :func:`content_fits` can say nothing useful. Mapping it at zero
    opacity forces a real layout pass while keeping it completely invisible --
    which matters because the alternative, briefly flashing a window on
    someone's desktop, is not acceptable for a self-test.
    """
    was_withdrawn = False
    previous_alpha = None
    try:
        was_withdrawn = window.state() == "withdrawn"
    except Exception:  # noqa: BLE001
        pass

    try:
        if was_withdrawn:
            try:
                previous_alpha = window.attributes("-alpha")
                window.attributes("-alpha", 0.0)
            except Exception:  # noqa: BLE001 - not every platform supports alpha
                pass
            window.deiconify()
        # Same reason as reveal_fitted: the DPI-scaled font metrics only apply
        # after the window is mapped, so one update is not a real layout.
        settle(window)
        yield window
    finally:
        if was_withdrawn:
            try:
                window.withdraw()
            except Exception:  # noqa: BLE001
                pass
            if previous_alpha is not None:
                try:
                    window.attributes("-alpha", previous_alpha)
                except Exception:  # noqa: BLE001
                    pass
