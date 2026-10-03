"""Turn what the user typed into a profile name."""

from __future__ import annotations

from typing import Any, Sequence


class ProfileNotFound(SystemExit):
    pass


def resolve(name: str, names: Sequence[str], cfg: dict[str, Any]) -> str:
    """Exact name, alias, email address, then unique prefix / substring."""
    if name in names:
        return name
    alias = (cfg.get("aliases") or {}).get(name)
    if alias in names:
        return alias
    slug = name.replace("@", "_")
    if slug in names:
        return slug
    for matcher in (lambda n: n.startswith(name), lambda n: name in n):
        matches = [n for n in names if matcher(n)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ProfileNotFound(f"Error: profile '{name}' is ambiguous: {', '.join(matches)}")
    known = ", ".join(names) or "none (add one with `mycodex profile add`)"
    raise ProfileNotFound(f"Error: profile '{name}' does not exist (profiles: {known})")


def aliases_for(name: str, cfg: dict[str, Any]) -> list[str]:
    return sorted(alias for alias, target in (cfg.get("aliases") or {}).items() if target == name)
