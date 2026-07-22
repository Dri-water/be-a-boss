"""Short, distinct coder names — a coder's identity within its thread."""

from __future__ import annotations

CODER_NAMES = [
    "Nova", "Kite", "Juno", "Vega", "Rook", "Wren", "Lyra", "Onyx",
    "Iris", "Moss", "Flint", "Sage", "Ember", "Pax", "Quill", "Zephyr",
]


def pick_name(taken: set[str]) -> str:
    for n in CODER_NAMES:
        if n.lower() not in taken:
            return n
    i = 2
    while True:
        for n in CODER_NAMES:
            cand = f"{n}{i}"
            if cand.lower() not in taken:
                return cand
        i += 1
