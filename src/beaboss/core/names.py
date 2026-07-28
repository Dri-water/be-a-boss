"""Worker display names and their Git/filesystem-safe identities."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence


DEFAULT_WORKER_NAMES = (
    "Nova", "Kite", "Juno", "Vega", "Rook", "Wren", "Lyra", "Onyx",
    "Iris", "Moss", "Flint", "Sage", "Ember", "Pax", "Quill", "Zephyr",
)

# Retain the old public constant name for callers importing it directly.
WORKER_NAMES = DEFAULT_WORKER_NAMES


def parse_worker_names(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated override, falling back when it has no names."""
    if not raw or not raw.strip():
        return DEFAULT_WORKER_NAMES

    names: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        name = " ".join(item.split())
        key = name.casefold()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
    return tuple(names) or DEFAULT_WORKER_NAMES


def worker_id_for(name: str) -> str:
    """Return a stable lowercase slug suitable for a path and Git branch."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    worker_id = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return worker_id or "worker"


def pick_name(
    taken: set[str],
    names: Sequence[str] = DEFAULT_WORKER_NAMES,
) -> str:
    """Choose the first display name whose display name and slug are both free."""
    normalized_taken = {item.casefold() for item in taken}
    for name in names:
        if name.casefold() not in normalized_taken and worker_id_for(name) not in normalized_taken:
            return name

    i = 2
    while True:
        for name in names:
            candidate = f"{name}{i}"
            if (
                candidate.casefold() not in normalized_taken
                and worker_id_for(candidate) not in normalized_taken
            ):
                return candidate
        i += 1
