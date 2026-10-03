"""Small formatting helpers shared by the command modules."""

from __future__ import annotations

import time
from datetime import datetime

from . import ui
from .quota import Quota


def reset_time(epoch: int | float | None) -> str:
    if not epoch:
        return "-"
    moment = datetime.fromtimestamp(int(epoch))
    now = datetime.now()
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if 0 <= (moment - now).days < 6:
        return moment.strftime("%a %H:%M")
    return moment.strftime("%Y-%m-%d")


def windows(q: Quota | None) -> str:
    if q is None:
        return "-"
    if not q.ok:
        return q.status
    return " | ".join(f"{w.name} {w.remaining:.0f}%" for w in q.windows) or "-"


def resets(q: Quota | None) -> str:
    if q is None or not q.ok:
        return "-"
    return " | ".join(f"{w.name} {reset_time(w.reset_at)}" for w in q.windows) or "-"


def age(epoch: float | None) -> str:
    if not epoch:
        return "-"
    from .procs import human_uptime
    return human_uptime(time.time() - float(epoch)) + " ago"


def status_style(text: str) -> ui.Style:
    return ui.value_style("Status", text)
