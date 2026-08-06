"""Optional Docker network shaping configured by ``BEABOSS_NETWORK_LIMIT``.

Upload traffic can be queued directly on the container interface. Linux ingress
qdiscs cannot queue, so downloads are redirected through an IFB device. Each
direction uses HTB for the ceiling and FQ-CoDel below it so one large Codex flow
cannot hold small Telegram/control packets behind a single FIFO backlog.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Protocol


_RATE = re.compile(
    r"^[0-9]+(?:[.][0-9]+)?(?:bit|kbit|mbit|gbit|bps|kbps|mbps|gbps)$"
)
IFB_DEVICE = "ifb-boss"
BURST = "64kb"


class Runner(Protocol):
    def __call__(
        self, args: Sequence[str], *, capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


def _run(
    args: Sequence[str], *, capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), check=True, text=True, capture_output=capture_output,
    )


def parse_limit(raw: str | None) -> str | None:
    """Return a validated tc rate; blank is deliberately uncapped."""
    value = (raw or "").strip()
    if not value:
        return None
    if not _RATE.fullmatch(value):
        raise ValueError(
            f"Invalid BEABOSS_NETWORK_LIMIT {value!r} (example: 3mbit)"
        )
    return value


def _default_interface(runner: Runner) -> str:
    result = runner(
        ["ip", "route", "show", "default"], capture_output=True,
    )
    fields = result.stdout.split()
    try:
        return fields[fields.index("dev") + 1]
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            "BEABOSS_NETWORK_LIMIT is set, but no default interface was found"
        ) from exc


def configure_network_limit(
    environ: Mapping[str, str] | None = None,
    runner: Runner = _run,
) -> tuple[str | None, str | None, str] | None:
    """Install idempotent fair queues, returning ``(upload, download, iface)``."""
    env = os.environ if environ is None else environ
    legacy = parse_limit(env.get("BEABOSS_NETWORK_LIMIT"))
    upload = (
        parse_limit(env.get("BEABOSS_NETWORK_UPLOAD_LIMIT"))
        if "BEABOSS_NETWORK_UPLOAD_LIMIT" in env else legacy
    )
    download = (
        parse_limit(env.get("BEABOSS_NETWORK_DOWNLOAD_LIMIT"))
        if "BEABOSS_NETWORK_DOWNLOAD_LIMIT" in env else legacy
    )
    if upload is None and download is None:
        return None

    interface = _default_interface(runner)
    if upload is not None:
        _install_fair_queue(interface, upload, runner)

    if download is not None:
        try:
            runner(
                ["ip", "link", "show", "dev", IFB_DEVICE],
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            runner(["ip", "link", "add", IFB_DEVICE, "type", "ifb"])
        runner(["ip", "link", "set", "dev", IFB_DEVICE, "up"])
        _install_fair_queue(IFB_DEVICE, download, runner)
        _delete_qdisc(interface, ["ingress"], runner)
        runner([
            "tc", "qdisc", "add", "dev", interface,
            "handle", "ffff:", "ingress",
        ])
        runner([
            "tc", "filter", "replace", "dev", interface, "parent", "ffff:",
            "protocol", "all", "pref", "1", "u32", "match", "u32", "0", "0",
            "action", "mirred", "egress", "redirect", "dev", IFB_DEVICE,
        ])
    return upload, download, interface


def _install_fair_queue(device: str, rate: str, runner: Runner) -> None:
    """Apply an aggregate ceiling with per-flow latency control below it."""
    # `tc qdisc replace ... htb` is not reliably idempotent once the existing HTB
    # tree has classes (some kernels return EOPNOTSUPP). Explicitly remove only our
    # root tree, tolerate first-run absence, then rebuild it deterministically.
    _delete_qdisc(device, ["root"], runner)
    runner([
        "tc", "qdisc", "add", "dev", device, "root", "handle", "1:",
        "htb", "default", "10", "r2q", "100",
    ])
    runner([
        "tc", "class", "replace", "dev", device, "parent", "1:",
        "classid", "1:10", "htb", "rate", rate, "ceil", rate,
        "burst", BURST, "cburst", BURST,
    ])
    runner([
        "tc", "qdisc", "replace", "dev", device, "parent", "1:10",
        "handle", "10:", "fq_codel",
    ])


def _delete_qdisc(device: str, selector: list[str], runner: Runner) -> None:
    try:
        runner(
            ["tc", "qdisc", "del", "dev", device, *selector],
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        pass


def main() -> None:
    try:
        configured = configure_network_limit()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
    if configured is not None:
        upload, download, interface = configured
        print(
            f"be-a-boss network traffic capped on {interface}: "
            f"upload={upload or 'uncapped'}, download={download or 'uncapped'} "
            "(HTB + FQ-CoDel)",
            flush=True,
        )


if __name__ == "__main__":
    main()
