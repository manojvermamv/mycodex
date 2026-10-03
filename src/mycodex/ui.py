"""Terminal rendering that follows prodex's own design rules.

Rules mirrored from prodex 0.435.1 (crates/prodex-terminal-ui/src/panel.rs and
profile_commands/manage.rs):
  * interactive stdout -> inline panel: header box with a bold cyan title, cyan border,
    header joined to the body with ├ ┤, body closed with └ ┘;
  * non-interactive stdout -> "[ Title ] ====" header plus aligned "Label: value" rows,
    width = terminal width or 110, label column 10..24;
  * palette: title/border/hint cyan, secondary gray, success green, error red,
    accent light cyan, tool light magenta; values green for active/yes, cyan for
    provider/auth/identity/route, red for missing/error.
"""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence, TextIO

CLI_WIDTH = 110
CLI_MIN_WIDTH = 60
CLI_LABEL_WIDTH = 16
CLI_MIN_LABEL_WIDTH = 10
CLI_MAX_LABEL_WIDTH = 24
TABLE_GAP = "  "

# ratatui named colours as crossterm renders them (38;5;N).
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GRAY = range(8)
DARK_GRAY, LIGHT_RED, LIGHT_GREEN, LIGHT_YELLOW, LIGHT_BLUE, LIGHT_MAGENTA, LIGHT_CYAN, WHITE = range(8, 16)


@dataclass(frozen=True)
class Style:
    fg: int | None = None
    bold: bool = False
    underline: bool = False

    def __call__(self, text: str, enabled: bool = True) -> str:
        if not enabled or not text or (self.fg is None and not self.bold and not self.underline):
            return text
        codes = []
        if self.bold:
            codes.append("1")
        if self.underline:
            codes.append("4")
        if self.fg is not None:
            codes.append(f"38;5;{self.fg}")
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


TITLE = Style(CYAN, bold=True)
BORDER = Style(CYAN)
HINT = Style(CYAN)
SECONDARY = Style(GRAY)
LABEL = Style(GRAY, bold=True)
PRIMARY = Style()
SUCCESS = Style(GREEN)
ERROR = Style(RED)
WARNING = Style(YELLOW)
ACCENT = Style(LIGHT_CYAN)
TOOL = Style(LIGHT_MAGENTA)
BOLD = Style(bold=True)
HEADING = Style(bold=True, underline=True)

Segment = tuple[str, Style]
Line = list[Segment]


# ----------------------------------------------------------------------------- terminal
def is_tty(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def color_enabled(stream: TextIO | None = None) -> bool:
    forced = os.environ.get("MYCODEX_COLOR", "").lower()
    if forced in ("always", "1", "yes"):
        return True
    if forced in ("never", "0", "no") or "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return is_tty(stream)


def interactive(stream: TextIO | None = None) -> bool:
    """Box-drawn panels only for a real terminal (prodex: is_terminal && !CODEX_CI)."""
    return is_tty(stream) and "CODEX_CI" not in os.environ and color_enabled(stream)


def width(stream: TextIO | None = None) -> int:
    override = os.environ.get("MYCODEX_WIDTH")
    if override and override.isdigit() and int(override) > 0:
        return int(override)
    if is_tty(stream):
        cols = shutil.get_terminal_size((CLI_WIDTH, 24)).columns
        return max(cols, 1)
    return CLI_WIDTH


# ----------------------------------------------------------------------------- text
def text_width(text: str) -> int:
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def fit(text: str, limit: int) -> str:
    """Truncate to `limit` columns with an ellipsis (prodex fit_cell)."""
    if limit <= 0:
        return ""
    if text_width(text) <= limit:
        return text
    out, used = [], 0
    for ch in text:
        w = text_width(ch)
        if used + w > limit - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - text_width(text))


def wrap(text: str, limit: int) -> list[str]:
    limit = max(1, limit)
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        current = ""
        for word in raw.split(" "):
            candidate = f"{current} {word}" if current else word
            if text_width(candidate) <= limit:
                current = candidate
                continue
            if current:
                lines.append(current)
            while text_width(word) > limit:
                cut, used = [], 0
                for ch in word:
                    if used + text_width(ch) > limit:
                        break
                    cut.append(ch)
                    used += text_width(ch)
                lines.append("".join(cut))
                word = word[len(cut):]
            current = word
        lines.append(current)
    return lines


def render_line(line: Line, enabled: bool) -> str:
    return "".join(style(text, enabled) for text, style in line)


def line_width(line: Line) -> int:
    return sum(text_width(text) for text, _ in line)


def wrap_line(line: Line, limit: int) -> list[Line]:
    """Wrap a styled line at `limit` columns, keeping segment styles."""
    if line_width(line) <= limit:
        return [line]
    out: list[Line] = []
    current: Line = []
    used = 0
    for text, style in line:
        for ch in text:
            w = text_width(ch)
            if used + w > limit:
                out.append(current)
                current, used = [], 0
            if current and current[-1][1] == style:
                current[-1] = (current[-1][0] + ch, style)
            else:
                current.append((ch, style))
            used += w
    if current:
        out.append(current)
    return out


# ----------------------------------------------------------------------------- value colours
GOOD_WORDS = ("ready", "ok", "yes", "active", "connected", "enabled", "running", "available",
              "logged in", "paired", "healthy", "up to date", "valid")
BAD_WORDS = ("no active", "missing", "error", "invalid", "expired", "failed", "exhausted",
             "limited", "blocked", "dead", "stopped", "unauthorized", "revoked", "conflict")
WARN_WORDS = ("warning", "degraded", "connecting", "unknown", "stale", "unavailable", "disabled",
              "not running", "inactive", "free", "pending", "untested")


def value_style(label: str, value: str) -> Style:
    lower = value.lower()
    if any(word in lower for word in BAD_WORDS):
        return ERROR
    if any(word in lower for word in WARN_WORDS):
        return WARNING
    if any(lower == word or lower.startswith(word) for word in GOOD_WORDS) or label in ("Active",):
        return SUCCESS
    if label in ("Provider", "Auth", "Runtime route", "Identity", "Account", "Plan", "Mode"):
        return Style(CYAN)
    return PRIMARY


# ----------------------------------------------------------------------------- output
def out(text: str = "", stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    try:
        stream.write(text + "\n")
        stream.flush()
    except BrokenPipeError:  # e.g. `mycodex status | head`
        pass


def section_header(title: str, total: int | None = None) -> str:
    total = total or width()
    prefix = f"[ {title} ] "
    used = text_width(prefix)
    if used >= total:
        return fit(prefix, total)
    return prefix + "=" * (total - used)


def label_width_for(fields: Sequence[tuple[str, str]], total: int) -> int:
    longest = max((text_width(label) + 1 for label, _ in fields), default=CLI_LABEL_WIDTH)
    max_by_width = max(1, min(total - 20, CLI_MAX_LABEL_WIDTH)) if total > 21 else 1
    preferred_cap = max(1, min(total // 4, CLI_MAX_LABEL_WIDTH))
    cap = min(max_by_width, preferred_cap)
    return max(min(CLI_MIN_LABEL_WIDTH, cap), min(longest, cap))


def plain_field_lines(label: str, value: str, total: int, lw: int) -> list[str]:
    tag = f"{label}:"
    lines = []
    for i, part in enumerate(wrap(value, max(1, total - (lw + 1)))):
        head = tag if i == 0 else ""
        lines.append(f"{head}{' ' * max(0, lw - text_width(head))} {part}")
    return lines


def _box(title: Line, body: list[Line], stream: TextIO, footer: Line | None = None) -> None:
    total = max(width(stream), 20)
    inner = total - 2
    enabled = color_enabled(stream)
    b = lambda s: BORDER(s, enabled)  # noqa: E731
    out(b("┌" + "─" * inner + "┐"), stream)
    for chunk in wrap_line(title, inner):
        out(b("│") + render_line(chunk, enabled) + " " * (inner - line_width(chunk)) + b("│"), stream)
    out(b("├" + "─" * inner + "┤"), stream)
    for line in body:
        for chunk in wrap_line(line, inner) if line else [[]]:
            out(b("│") + render_line(chunk, enabled) + " " * (inner - line_width(chunk)) + b("│"), stream)
    if footer:
        out(b("├" + "─" * inner + "┤"), stream)
        for chunk in wrap_line(footer, inner):
            out(b("│") + render_line(chunk, enabled) + " " * (inner - line_width(chunk)) + b("│"), stream)
    out(b("└" + "─" * inner + "┘"), stream)


def title_line(title: str, subtitle: str | None = None) -> Line:
    line: Line = [(title, TITLE)]
    if subtitle:
        line += [("  ", PRIMARY), (subtitle, SECONDARY)]
    return line


def panel(title: str, fields: Sequence[tuple[str, str]], subtitle: str | None = None,
          stream: TextIO | None = None, styler=value_style) -> None:
    """Single key/value panel (prodex print_panel)."""
    stream = stream or sys.stdout
    total = width(stream)
    lw = label_width_for(fields, total)
    if interactive(stream):
        body: list[Line] = []
        for label, value in fields:
            for i, part in enumerate(wrap(value, max(1, total - 2 - lw - 1))):
                head = f"{label}:" if i == 0 else ""
                body.append([(pad(head, lw) + " ", SECONDARY), (part, styler(label, value))])
        _box(title_line(title, subtitle), body, stream)
        return
    out(section_header(title if not subtitle else f"{title}  {subtitle}", total), stream)
    for label, value in fields:
        for line in plain_field_lines(label, value, total, lw):
            out(line, stream)


def multi_panel(title: str, sections: Sequence[tuple[str, Sequence[tuple[str, str]]]],
                stream: TextIO | None = None, styler=value_style) -> None:
    """Several titled field groups in one box (prodex profile list)."""
    stream = stream or sys.stdout
    if interactive(stream):
        body: list[Line] = []
        for section_title, fields in sections:
            body.append([(section_title, TITLE)])
            lw = min(max((text_width(label) for label, _ in fields), default=0), 22)
            for label, value in fields:
                body.append([(pad(label, lw) + " ", LABEL), (value, styler(label, value))])
        _box(title_line(title, f"{len(sections)} panel(s)"), body, stream)
        return
    for section_title, fields in sections:
        panel(section_title, fields, stream=stream, styler=styler)


@dataclass
class Column:
    header: str
    min_width: int = 4
    max_width: int | None = None
    align: str = "left"


def table(title: str, columns: Sequence[Column], rows: Sequence[Sequence[str]],
          subtitle: str | None = None, footer: str | None = None, stream: TextIO | None = None,
          cell_style=None, notes: Sequence[str] = ()) -> None:
    """Column table (prodex session list layout; boxed on a terminal)."""
    stream = stream or sys.stdout
    total = width(stream)
    avail = total - (2 if interactive(stream) else 0)
    widths = []
    for i, col in enumerate(columns):
        w = max([text_width(col.header)] + [text_width(r[i]) for r in rows] + [col.min_width])
        if col.max_width:
            w = min(w, col.max_width)
        widths.append(w)
    gap = text_width(TABLE_GAP)
    while sum(widths) + gap * (len(widths) - 1) > avail:
        shrinkable = [i for i, w in enumerate(widths) if w > max(6, columns[i].min_width)]
        if not shrinkable:
            break
        widths[max(shrinkable, key=lambda i: widths[i])] -= 1

    def cells(values: Sequence[str], styles: Sequence[Style]) -> Line:
        line: Line = []
        for i, (value, w) in enumerate(zip(values, widths)):
            text = fit(value, w)
            text = (" " * (w - text_width(text)) + text) if columns[i].align == "right" else pad(text, w)
            if i:
                line.append((TABLE_GAP, PRIMARY))
            line.append((text, styles[i]))
        return line

    header = cells([c.header for c in columns], [LABEL] * len(columns))
    body_rows = []
    for row in rows:
        styles = [cell_style(i, row) if cell_style else PRIMARY for i in range(len(columns))]
        body_rows.append(cells(row, styles))
    if interactive(stream):
        body = [header] + body_rows
        for note in notes:
            body.append([(note, SECONDARY)])
        _box(title_line(title, subtitle), body, stream, [(footer, HINT)] if footer else None)
        return
    out(section_header(title if not subtitle else f"{title}  {subtitle}", total), stream)
    out(render_line(header, False).rstrip(), stream)
    out("-" * min(total, sum(widths) + gap * (len(widths) - 1)), stream)
    for line in body_rows:
        out(render_line(line, False).rstrip(), stream)
    for note in notes:
        out(note, stream)
    if footer:
        out(footer, stream)


# ----------------------------------------------------------------------------- messages
def error(message: str, hints: Iterable[str] = ()) -> None:
    enabled = color_enabled(sys.stderr)
    out(f"{ERROR('Error:', enabled)} {message}", sys.stderr)
    for hint in hints:
        out(f"  {HINT('hint:', enabled)} {hint}", sys.stderr)


def warn(message: str) -> None:
    enabled = color_enabled(sys.stderr)
    out(f"{WARNING('Warning:', enabled)} {message}", sys.stderr)


def info(message: str, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    out(f"{HINT('mycodex:', color_enabled(stream))} {message}", stream)


def success(message: str, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    out(f"{SUCCESS('✓', color_enabled(stream))} {message}", stream)


def failure(message: str, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    out(f"{ERROR('✗', color_enabled(stream))} {message}", stream)


def confirm(question: str, default: bool = False, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not is_tty(sys.stdin):
        raise SystemExit(f"Error: {question} (refusing without a terminal; pass --yes)")
    enabled = color_enabled(sys.stdout)
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{HINT('?', enabled)} {BOLD(question, enabled)} {SECONDARY(suffix, enabled)} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def choose(title: str, options: Sequence[tuple[str, str]], default: int = 0) -> str:
    """Numbered selection menu; returns the chosen option key."""
    if not is_tty(sys.stdin):
        raise SystemExit(f"Error: {title}: selection needs a terminal; pass the value explicitly")
    enabled = color_enabled(sys.stdout)
    out(TITLE(title, enabled))
    for i, (_, label) in enumerate(options, start=1):
        marker = HINT("›", enabled) if i - 1 == default else " "
        out(f" {marker} {BOLD(str(i), enabled)}. {label}")
    while True:
        try:
            raw = input(f"{HINT('?', enabled)} choice {SECONDARY(f'[{default + 1}]', enabled)} ").strip()
        except EOFError:
            raw = ""
        if not raw:
            return options[default][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        out(ERROR(f"enter 1-{len(options)}", enabled))


class StatusBox:
    """In-place status panel like prodex's launch preflight box (`Title  phase` / `Status msg`)."""

    def __init__(self, title: str, stream: TextIO | None = None):
        self.title = title
        self.stream = stream or sys.stderr
        self.drawn = 0
        self.fancy = interactive(self.stream)
        self.last = None

    def update(self, phase: str, message: str) -> None:
        if (phase, message) == self.last:
            return
        self.last = (phase, message)
        if not self.fancy:
            out(f"{self.title}: {phase}: {message}", self.stream)
            return
        total = max(width(self.stream), 20)
        inner = total - 2
        title = fit(self.title, inner)
        phase_text = fit(f"  {phase}", max(0, inner - text_width(title)))
        top = (BORDER("┌") + TITLE(title) + SECONDARY(phase_text)
               + BORDER("─" * max(0, inner - text_width(title) - text_width(phase_text)) + "┐"))
        msg = fit(message, max(1, inner - 7))
        mid = (BORDER("│") + SECONDARY("Status ") + msg
               + " " * max(0, inner - 7 - text_width(msg)) + BORDER("│"))
        bottom = BORDER("└" + "─" * inner + "┘")
        if self.drawn:
            self.stream.write(f"\x1b[{self.drawn}F\x1b[J")
        self.stream.write(top + "\n" + mid + "\n" + bottom + "\n")
        self.stream.flush()
        self.drawn = 3

    def clear(self) -> None:
        if self.fancy and self.drawn:
            self.stream.write(f"\x1b[{self.drawn}F\x1b[J")
            self.stream.flush()
            self.drawn = 0


# ----------------------------------------------------------------------------- help
def help_text(description: str, usage: str, commands: Sequence[tuple[str, str]],
              options: Sequence[tuple[str, str]], tips: Sequence[str] = (),
              examples: Sequence[str] = (), stream: TextIO | None = None) -> None:
    """clap-style help as prodex prints it (bold+underline headings, bold literals)."""
    stream = stream or sys.stdout
    en = color_enabled(stream)
    out(description, stream)
    out("", stream)
    out(f"{HEADING('Usage:', en)} {usage}", stream)
    if commands:
        out("", stream)
        out(HEADING("Commands:", en), stream)
        w = max(text_width(c) for c, _ in commands)
        for cmd, desc in commands:
            out(f"  {BOLD(pad(cmd, w), en)}  {desc}", stream)
    if options:
        out("", stream)
        out(HEADING("Options:", en), stream)
        w = max(text_width(o) for o, _ in options)
        for opt, desc in options:
            out(f"  {BOLD(pad(opt, w), en)}  {desc}", stream)
    if tips:
        out("", stream)
        out("Tips:", stream)
        for tip in tips:
            out(f"  {tip}", stream)
    if examples:
        out("", stream)
        out("Examples:", stream)
        for example in examples:
            out(f"  {example}", stream)
