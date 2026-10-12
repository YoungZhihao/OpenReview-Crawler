"""Typeset the LaTeX that appears in reviews as MathML.

Why MathML rather than KaTeX or MathJax
---------------------------------------
OpenReview itself renders maths with KaTeX, and KaTeX is the gold standard.
Bundling it costs roughly 800 KB of JavaScript and base64 web fonts *per
report*, which would turn a 200 KB self-contained file into a 1 MB one.

MathML gets nearly the same quality for free: every current browser ships a
real maths layout engine, so fractions get a proper bar, radicals get a proper
sign, and large operators get their limits above and below in display mode.
The report stays a single small file with no scripts and no fonts to load.
Verified side by side against the previous CSS approach: subscripts, fraction
bars, radical signs and summation limits were all visibly better.

The original LaTeX is preserved in an ``<annotation>`` (the standard MathML
convention) and on the wrapper element, so a rendering can always be checked
against its source, and the "show source" toggle still works.

LaTeX to MathML by hand
-----------------------
A key detail: ``\\mathcal{S}`` and ``\\mathbb{R}`` are emitted as the real
Unicode mathematical alphanumeric characters (𝒮, ℝ) rather than relying on
``mathvariant``, which MathML Core no longer honours in every browser. The
Unicode ranges have gaps -- script B, E, F, H, I, L, M, R live in Letterlike
Symbols instead -- so those are mapped explicitly; guessing from the range
would silently produce the wrong glyph.

Anything structural this cannot express (``\\begin{}`` environments, matrices
beyond a simple table) falls back to showing the source rather than guessing.
"""

from __future__ import annotations

import html
import re

# --------------------------------------------------------------------------- #
# symbol tables
# --------------------------------------------------------------------------- #
GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
}

SYMBOLS = {
    "times": "×", "cdot": "⋅", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗",
    "star": "⋆", "circ": "∘", "bullet": "•", "oplus": "⊕", "ominus": "⊖",
    "otimes": "⊗", "oslash": "⊘", "odot": "⊙", "wedge": "∧", "vee": "∨",
    "cap": "∩", "cup": "∪", "setminus": "∖", "sqcup": "⊔", "sqcap": "⊓",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "sim": "∼", "simeq": "≃", "cong": "≅", "equiv": "≡",
    "propto": "∝", "ll": "≪", "gg": "≫", "subset": "⊂", "supset": "⊃",
    "subseteq": "⊆", "supseteq": "⊇", "in": "∈", "notin": "∉", "ni": "∋",
    "prec": "≺", "succ": "≻", "perp": "⊥", "parallel": "∥", "mid": "∣",
    "models": "⊨", "vdash": "⊢",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "mapsto": "↦", "uparrow": "↑", "downarrow": "↓", "nearrow": "↗",
    "searrow": "↘", "longrightarrow": "⟶", "longleftarrow": "⟵",
    "implies": "⟹", "iff": "⟺",
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "oint": "∮",
    "iint": "∬", "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⨁",
    "bigotimes": "⨂", "bigwedge": "⋀", "bigvee": "⋁",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉",
    "lfloor": "⌊", "rfloor": "⌋", "lVert": "‖", "rVert": "‖",
    "lvert": "|", "rvert": "|", "backslash": "\\", "vert": "|",
    "infty": "∞", "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃",
    "nexists": "∄", "emptyset": "∅", "varnothing": "∅", "angle": "∠",
    "triangle": "△", "square": "□", "neg": "¬", "lnot": "¬", "top": "⊤",
    "bot": "⊥", "ell": "ℓ", "hbar": "ℏ", "wp": "℘",
    "aleph": "ℵ", "prime": "′", "ldots": "…", "dots": "…", "cdots": "⋯",
    "vdots": "⋮", "ddots": "⋱", "surd": "√", "checkmark": "✓",
    "degree": "°", "pounds": "£", "euro": "€", "S": "§", "P": "¶",
    "dagger": "†", "ddagger": "‡", "blacksquare": "■", "Box": "□",
}

# Large operators whose limits TeX places above and below in display style.
LIMIT_OPERATORS = {
    "sum", "prod", "coprod", "bigcup", "bigcap", "bigoplus", "bigotimes",
    "bigwedge", "bigvee",
}

UPRIGHT_WORDS = {
    "log", "ln", "lg", "exp", "sin", "cos", "tan", "cot", "sec", "csc",
    "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "min", "max",
    "arg", "lim", "limsup", "liminf", "det", "dim", "ker", "deg", "gcd",
    "lcm", "Pr", "sup", "inf", "mod", "bmod", "sgn", "rank", "tr", "diag",
    "Var", "Cov", "softmax", "sigmoid", "relu", "KL", "E",
}

TEXT_COMMANDS = {"text", "textrm", "textbf", "textit", "mbox", "operatorname",
                 "textnormal", "textsc", "texttt"}

FONT_COMMANDS = {
    "mathcal": "script", "mathscr": "script", "mathbb": "double-struck",
    "mathbf": "bold", "boldsymbol": "bold", "bm": "bold",
    "mathrm": "normal", "mathsf": "sans-serif", "mathtt": "monospace",
    "mathit": "italic", "mathfrak": "fraktur",
}

ACCENTS = {
    "hat": "^", "widehat": "^", "bar": "¯", "overline": "¯",
    "tilde": "~", "widetilde": "~", "dot": "˙", "ddot": "¨",
    "vec": "→", "check": "ˇ", "breve": "˘", "acute": "´", "grave": "`",
}

SPACES = {
    ",": "0.17em", ":": "0.22em", ";": "0.28em", " ": "0.25em",
    "quad": "1em", "qquad": "2em", "enspace": "0.5em",
    "thinspace": "0.17em", "medspace": "0.22em", "thickspace": "0.28em",
}

UNSUPPORTED = (
    "begin", "end", "over", "atop", "choose", "genfrac", "substack",
    "matrix", "cases", "aligned", "align", "array", "tabular",
)

_COMMAND = re.compile(r"\\([A-Za-z]+|.)")


class UnsupportedMath(Exception):
    """Raised when an expression uses a construct we must not guess at."""


# --------------------------------------------------------------------------- #
# Unicode mathematical alphanumerics
# --------------------------------------------------------------------------- #
# The ranges are not contiguous: a handful of letters live in Letterlike
# Symbols, so the gaps have to be named explicitly. Deriving them from the
# range would silently give the wrong glyph.
# (upper-case start, lower-case start) for each style. The upper-case run is
# 26 long; only the two starts are needed.
_BLOCKS = {
    "bold": (0x1D400, 0x1D41A),
    "italic": (0x1D434, 0x1D44E),
    "script": (0x1D49C, 0x1D4B6),
    "fraktur": (0x1D504, 0x1D51E),
    "double-struck": (0x1D538, 0x1D552),
    "sans-serif": (0x1D5A0, 0x1D5BA),
    "monospace": (0x1D670, 0x1D68A),
}

# Digit runs exist only for these styles.
_DIGITS = {
    "bold": 0x1D7CE, "double-struck": 0x1D7D8,
    "sans-serif": 0x1D7E2, "monospace": 0x1D7F6,
}

_HOLES = {
    "script": {
        "B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ",
        "M": "ℳ", "R": "ℛ", "e": "ℯ", "g": "ℊ", "o": "ℴ",
    },
    "fraktur": {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"},
    "double-struck": {
        "C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ",
    },
}


def variant_char(char: str, style: str) -> str:
    """The mathematical alphanumeric for ``char`` in ``style``, else ``char``."""
    if style == "normal":
        return char
    hole = _HOLES.get(style, {}).get(char)
    if hole:
        return hole
    block = _BLOCKS.get(style)
    if not block:
        return char
    upper_start, lower_start = block
    if "A" <= char <= "Z":
        return chr(upper_start + (ord(char) - ord("A")))
    if "a" <= char <= "z":
        return chr(lower_start + (ord(char) - ord("a")))
    digits_start = _DIGITS.get(style)
    if digits_start and "0" <= char <= "9":
        return chr(digits_start + (ord(char) - ord("0")))
    return char


# --------------------------------------------------------------------------- #
# node model
# --------------------------------------------------------------------------- #
class _Node:
    """A MathML element: a tag, optional attributes, and children."""

    __slots__ = ("tag", "children", "attrs")

    def __init__(self, tag: str, children=(), **attrs: str) -> None:
        self.tag = tag
        self.children = list(children)
        self.attrs = attrs

    def xml(self) -> str:
        attributes = "".join(f' {k}="{html.escape(str(v), quote=True)}"'
                             for k, v in self.attrs.items())
        if not self.children:
            return f"<{self.tag}{attributes}></{self.tag}>"
        inner = "".join(child.xml() if isinstance(child, _Node)
                        else html.escape(str(child))
                        for child in self.children)
        return f"<{self.tag}{attributes}>{inner}</{self.tag}>"


def _row(nodes: list) -> _Node:
    if len(nodes) == 1 and isinstance(nodes[0], _Node):
        return nodes[0]
    return _Node("mrow", nodes)


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
DELIMITERS = (
    ("$$", "$$", True),
    ("\\[", "\\]", True),
    ("\\(", "\\)", False),
    ("$", "$", False),
)


def split_math(raw: str) -> tuple[str, bool] | None:
    for opening, closing, display in DELIMITERS:
        if raw.startswith(opening) and raw.endswith(closing) \
                and len(raw) >= len(opening) + len(closing) + 1:
            return raw[len(opening):len(raw) - len(closing)], display
    return None


def render_math(raw: str) -> str:
    """Render one delimited expression as MathML, or as escaped source."""
    parsed = split_math(raw)
    if parsed is None:
        return f'<span class="math math-raw">{html.escape(raw)}</span>'

    body, display = parsed
    classes = "math math-display" if display else "math"
    attributes = (f' title="{html.escape(raw, quote=True)}"'
                  f' data-tex="{html.escape(body, quote=True)}"')
    try:
        inner = render_mathml(body, display=display)
    except UnsupportedMath:
        # A formula the reader can read and re-parse beats one we garble.
        return (f'<span class="{classes} math-raw"{attributes}>'
                f"{html.escape(body.strip())}</span>")
    return f'<span class="{classes}"{attributes}>{inner}</span>'


def render_mathml(body: str, display: bool = False) -> str:
    """Convert a LaTeX maths body (no delimiters) to a MathML ``<math>``."""
    body = (body or "").strip()
    if not body:
        return ""
    for command in UNSUPPORTED:
        if re.search(r"\\" + command + r"\b", body):
            raise UnsupportedMath(command)
    if body.count("{") != body.count("}"):
        raise UnsupportedMath("unbalanced braces")

    rows = _split_rows(body)
    if len(rows) == 1 and len(rows[0]) == 1:
        content = _row(_MathParser(rows[0][0], display).parse())
    else:
        # Multi-line or aligned: MathML's table is exactly the right shape.
        content = _Node("mtable", [_table_row(cells) for cells in rows])

    # The source is kept in the standard MathML annotation slot: not rendered,
    # but available to assistive tech and to anyone checking the output.
    semantics = _Node("semantics", [
        content,
        _Node("annotation", [body], encoding="application/x-tex"),
    ])
    display_attribute = ' display="block"' if display else ""
    return (
        '<math xmlns="http://www.w3.org/1998/Math/MathML"'
        f"{display_attribute}>{semantics.xml()}</math>"
    )


def _split_rows(body: str) -> list[list[str]]:
    """Split on top-level ``\\\\`` into rows of ``&``-separated cells."""
    parts = re.split(r"\\\\", body)
    return [re.split(r"(?<!\\)&", part) for part in parts]


def _table_row(cells: list[str]) -> _Node:
    return _Node("mtr", [
        _Node("mtd", [_row(_MathParser(cell).parse())]) for cell in cells
    ])


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
class _MathParser:
    def __init__(self, tex: str, display: bool = False) -> None:
        self.tex = tex
        self.pos = 0
        self.display = display

    # -- primitives ----------------------------------------------------- #
    def eof(self) -> bool:
        return self.pos >= len(self.tex)

    def peek(self) -> str:
        return "" if self.eof() else self.tex[self.pos]

    def skip_space(self) -> None:
        while not self.eof() and self.tex[self.pos] in " \t\n":
            self.pos += 1

    def read_group(self) -> str:
        self.skip_space()
        if self.peek() != "{":
            raise UnsupportedMath("expected a brace group")
        depth = 0
        start = self.pos + 1
        index = self.pos
        while index < len(self.tex):
            char = self.tex[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    self.pos = index + 1
                    return self.tex[start:index]
            index += 1
        raise UnsupportedMath("unterminated brace group")

    def read_argument(self) -> str:
        self.skip_space()
        if self.eof():
            return ""
        if self.peek() == "{":
            return self.read_group()
        if self.peek() == "\\":
            match = _COMMAND.match(self.tex, self.pos)
            if match:
                self.pos = match.end()
                return match.group(0)
        char = self.tex[self.pos]
        self.pos += 1
        return char

    def read_optional(self) -> str | None:
        self.skip_space()
        if self.peek() != "[":
            return None
        end = self.tex.find("]", self.pos)
        if end < 0:
            return None
        inner = self.tex[self.pos + 1:end]
        self.pos = end + 1
        return inner

    def sub_parse(self, tex: str) -> list:
        return _MathParser(tex, self.display).parse()

    # -- main loop ------------------------------------------------------ #
    def parse(self, stop: str | None = None) -> list:
        out: list = []
        while not self.eof():
            char = self.peek()
            if stop is not None and char == stop:
                break
            if char == "{":
                out.append(_row(self.sub_parse(self.read_group())))
                continue
            if char == "}":
                self.pos += 1
                continue
            if char in "^_":
                self.pos += 1
                out = self._attach_script(out, char)
                continue
            if char == "\\":
                # An escaped underscore/caret ("$\mathcal{S}\_t$") is a script
                # marker, and it must attach to the *preceding* sibling -- so it
                # is intercepted here, where the output list is in scope.
                script = _COMMAND.match(self.tex, self.pos)
                if script and script.group(1) in ("_", "^"):
                    self.pos = script.end()
                    out = self._attach_script(out, script.group(1))
                    continue
                node = self.command()
                if node is not None:
                    out.append(node)
                continue
            if char == "&":
                self.pos += 1
                continue
            if char == "~":
                self.pos += 1
                out.append(_Node("mspace", width="0.25em"))
                continue
            if char.isspace():
                self.pos += 1
                out.append(" ")
                continue
            out.append(self.atom(char))
            self.pos += 1
        return out

    def _attach_script(self, out: list, marker: str) -> list:
        """Wrap the previous node in a script element.

        In display style a large operator takes its limits above and below, so
        ``munder``/``munderover`` is used there instead of ``msub``; that is
        what makes ``\\sum_{i}`` put the *i* under the sigma rather than beside
        it.
        """
        argument = self.read_argument()
        script = _row(self.sub_parse(argument)) if argument else _Node("mrow")
        if not out:
            return [_Node("msub" if marker == "_" else "msup",
                          [_Node("mrow"), script])]

        base = out.pop()
        if not isinstance(base, _Node):
            base = _Node("mi", [base])

        limits = self.display and base.attrs.get("movablelimits") == "true"

        if base.tag in ("msub", "msup", "msubsup"):
            # Merge a second script onto the same base.
            existing = base.children
            if base.tag == "msub":
                lower, upper = existing[1], script if marker == "^" else None
                base = _Node("munderover" if limits else "msubsup",
                             [existing[0],
                              lower if marker == "^" else script,
                              upper or _Node("mrow")])
            elif base.tag == "msup":
                base = _Node("munderover" if limits else "msubsup",
                             [existing[0],
                              script if marker == "_" else _Node("mrow"),
                              existing[1]])
            # msubsup already complete: leave as is.
            out.append(base)
            return out

        if limits:
            out.append(_Node("munder" if marker == "_" else "mover",
                             [base, script]))
        else:
            out.append(_Node("msub" if marker == "_" else "msup",
                             [base, script]))
        return out

    def atom(self, char: str) -> _Node:
        if char.isalpha():
            return _Node("mi", [char])
        if char.isdigit():
            return _Node("mn", [char])
        if char in "+−-=<>±×÷⋅*/":
            return _Node("mo", [char])
        if char in "()[]|":
            return _Node("mo", [char])
        if char == "-":
            return _Node("mo", ["−"])
        if char == "'":
            return _Node("mo", ["′"])
        if char in ",.;:!?":
            return _Node("mo", [char])
        return _Node("mo", [char])

    # -- commands ------------------------------------------------------- #
    def command(self) -> _Node | None:
        match = _COMMAND.match(self.tex, self.pos)
        if not match:
            self.pos += 1
            return _Node("mo", ["\\"])
        name = match.group(1)
        self.pos = match.end()

        if name == "\\":
            return _Node("mspace", linebreak="newline")
        # Reviews store escaped underscores ("$\mathcal{S}\_t$") because the
        # underscore had to survive the Markdown layer. Taking \_ literally
        # drops the subscript entirely, so here it is the subscript marker the
        # author meant.
        if name == "_":
            return self._sole_script("_")
        if name == "^":
            return self._sole_script("^")
        if name in ("%", "&", "#", "$", "{", "}"):
            return _Node("mo", [name])
        if name == "|":
            return _Node("mo", ["‖"])
        if name in SPACES:
            width = SPACES[name]
            if width.startswith("-"):
                return _Node("mspace", width="0.1em")
            return _Node("mspace", width=width)
        if name in GREEK:
            return _Node("mi", [GREEK[name]])
        if name in SYMBOLS:
            node = _Node("mo", [SYMBOLS[name]])
            if name in LIMIT_OPERATORS:
                node.attrs["movablelimits"] = "true"
            return node
        if name in UPRIGHT_WORDS:
            node = _Node("mo", [name])
            if name in ("min", "max", "lim", "sup", "inf", "arg"):
                node.attrs["movablelimits"] = "true"
            return node
        if name in TEXT_COMMANDS:
            return _Node("mtext", [self.read_argument()])
        if name in FONT_COMMANDS:
            style = FONT_COMMANDS[name]
            argument = self.read_argument()
            return _Node("mrow", self._styled(argument, style))
        if name in ACCENTS:
            argument = self.read_argument()
            return _Node("mover", [_row(self.sub_parse(argument)),
                                   _Node("mo", [ACCENTS[name]])],
                         accent="true")
        if name in ("frac", "dfrac", "tfrac"):
            numerator, denominator = self.read_group(), self.read_group()
            return _Node("mfrac", [_row(self.sub_parse(numerator)),
                                   _row(self.sub_parse(denominator))])
        if name == "sqrt":
            index = self.read_optional()
            argument = self.read_argument()
            radicand = _row(self.sub_parse(argument))
            if index:
                return _Node("mroot", [radicand,
                                       _row(self.sub_parse(index))])
            return _Node("msqrt", [radicand])
        if name in ("left", "right", "big", "Big", "bigg", "Bigg", "bigl",
                    "bigr", "Bigl", "Bigr", "middle"):
            delimiter = self.read_argument()
            return self.delimiter(delimiter)
        if name in ("limits", "nolimits", "displaystyle", "textstyle",
                    "scriptstyle", "scriptscriptstyle", "mathstrut", "!",
                    "/", "@", "relax", "hspace", "vspace"):
            if name == "hspace":
                self.read_argument()
            return None

        # Unknown command: render its name upright so the reader can see what
        # the author wrote rather than losing it.
        return _Node("mi", [name], mathvariant="normal")

    def _sole_script(self, marker: str) -> _Node:
        """A script command standing alone (``\_t`` with nothing before it)."""
        argument = self.read_argument()
        script = _row(self.sub_parse(argument)) if argument else _Node("mrow")
        return _Node("msub" if marker == "_" else "msup",
                     [_Node("mrow"), script])

    def _styled(self, argument: str, style: str) -> list:
        """Apply a font command, using real Unicode where one exists."""
        nodes = self.sub_parse(argument)
        if style in ("script", "double-struck", "fraktur"):
            # Emit the actual Unicode character: mathvariant is not honoured
            # consistently by MathML Core, and the glyph is what matters.
            mapped: list = []
            for node in nodes:
                if isinstance(node, _Node) and node.tag == "mi" \
                        and len(node.children) == 1 \
                        and isinstance(node.children[0], str):
                    mapped.append(_Node("mi", [variant_char(node.children[0],
                                                            style)]))
                else:
                    mapped.append(node)
            return mapped
        attribute = {"bold": "bold", "italic": "italic",
                     "sans-serif": "sans-serif",
                     "monospace": "monospace"}.get(style)
        if not attribute:
            return nodes
        return [_Node("mstyle", nodes, mathvariant=attribute)]

    def delimiter(self, source: str) -> _Node:
        if source.startswith("\\"):
            name = source[1:]
            if name in SYMBOLS:
                return _Node("mo", [SYMBOLS[name]])
            if name in ("lbrace", "rbrace"):
                return _Node("mo", ["{" if name == "lbrace" else "}"])
            return _Node("mo", [name])
        return _Node("mo", [source])
