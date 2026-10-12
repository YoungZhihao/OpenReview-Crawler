"""Console output helpers shared by the CLI and the tools.

Redirected stdout on Windows defaults to the ANSI code page (GBK on a Chinese
system), which raises ``UnicodeEncodeError`` the moment a ``✓`` or a Chinese
character is printed to a pipe, and reorders progress lines relative to stderr
because block buffering kicks in. Both are fixed in one place here.
"""

from __future__ import annotations

import sys


def fix_console() -> None:
    """Make redirected output UTF-8 and line-buffered.

    No-op for an interactive terminal: Python already speaks UTF-8 to a Windows
    console (PEP 528), and reconfiguring it there would only risk breaking a
    working setup.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace",
                                   line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass
