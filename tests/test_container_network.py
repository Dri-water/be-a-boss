import subprocess

import pytest

from beaboss.container_network import (
    IFB_DEVICE,
    configure_network_limit,
    parse_limit,
)


class FakeRunner:
    def __init__(self, *, ifb_exists: bool = False):
        self.ifb_exists = ifb_exists
        self.calls: list[list[str]] = []

    def __call__(self, args, *, capture_output=False):
        command = list(args)
        self.calls.append(command)
        if command == ["ip", "route", "show", "default"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="default via 172.20.0.1 dev eth0\n")
        if command == ["ip", "link", "show", "dev", IFB_DEVICE]:
            if not self.ifb_exists:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout="ifb-boss: up\n")
        return subprocess.CompletedProcess(command, 0, stdout="")


def test_blank_network_limit_is_uncapped_and_touches_nothing():
    runner = FakeRunner()
    assert configure_network_limit({}, runner) is None
    assert runner.calls == []


@pytest.mark.parametrize("value", ["3", "3 mbps", "fast", "3mbit; reboot"])
def test_network_limit_rejects_ambiguous_or_unsafe_values(value):
    with pytest.raises(ValueError, match="BEABOSS_NETWORK_LIMIT"):
        parse_limit(value)


def test_network_limit_queues_both_directions_without_ingress_policing():
    runner = FakeRunner()
    assert configure_network_limit(
        {"BEABOSS_NETWORK_LIMIT": "3mbit"}, runner,
    ) == ("3mbit", "3mbit", "eth0")

    flattened = [" ".join(call) for call in runner.calls]
    assert f"ip link add {IFB_DEVICE} type ifb" in flattened
    assert "tc qdisc del dev eth0 root" in flattened
    assert "tc qdisc add dev eth0 root handle 1: htb default 10 r2q 100" in flattened
    assert any("class replace dev eth0" in call and "rate 3mbit" in call
               and "ceil 3mbit" in call for call in flattened)
    assert "tc qdisc replace dev eth0 parent 1:10 handle 10: fq_codel" in flattened
    assert any(f"class replace dev {IFB_DEVICE}" in call
               and "rate 3mbit" in call and "ceil 3mbit" in call
               for call in flattened)
    assert (f"tc qdisc replace dev {IFB_DEVICE} parent 1:10 handle 10: fq_codel"
            in flattened)
    assert any(f"mirred egress redirect dev {IFB_DEVICE}" in call
               for call in flattened)
    assert not any(" police " in f" {call} " or " tbf " in f" {call} "
                   for call in flattened)


def test_upload_and_download_limits_can_be_configured_independently():
    runner = FakeRunner()
    assert configure_network_limit({
        "BEABOSS_NETWORK_UPLOAD_LIMIT": "12mbit",
        "BEABOSS_NETWORK_DOWNLOAD_LIMIT": "30mbit",
    }, runner) == ("12mbit", "30mbit", "eth0")

    flattened = [" ".join(call) for call in runner.calls]
    assert any("class replace dev eth0" in call and "rate 12mbit" in call
               and "ceil 12mbit" in call for call in flattened)
    assert any(f"class replace dev {IFB_DEVICE}" in call
               and "rate 30mbit" in call and "ceil 30mbit" in call
               for call in flattened)


def test_blank_explicit_download_disables_legacy_download_only():
    runner = FakeRunner()
    assert configure_network_limit({
        "BEABOSS_NETWORK_LIMIT": "3mbit",
        "BEABOSS_NETWORK_DOWNLOAD_LIMIT": "",
    }, runner) == ("3mbit", None, "eth0")
    assert not any(IFB_DEVICE in call for call in runner.calls)


def test_network_limit_reuses_existing_ifb_idempotently():
    runner = FakeRunner(ifb_exists=True)
    configure_network_limit({"BEABOSS_NETWORK_LIMIT": "750kbit"}, runner)
    assert ["ip", "link", "add", IFB_DEVICE, "type", "ifb"] not in runner.calls
    flattened = [" ".join(call) for call in runner.calls]
    assert "tc qdisc del dev eth0 root" in flattened
    assert f"tc qdisc del dev {IFB_DEVICE} root" in flattened
    assert "tc qdisc del dev eth0 ingress" in flattened
