"""LL-018 -- the rules scripts/no-third-party-check.sh judges a packet capture by.

The check itself needs Docker and runs in CI's `no-third-party` job. This pins, without Docker,
the classifier that decides what counts as sending something to someone other than the client
(scripts/no-third-party/classify.py). The lines below are `tcpdump -nn -A -r` output. Those
under "recorded" were taken from a passing run against the compose stack on 2026-09-23, some with
their TCP options trimmed; the leaks are the same shape with the addresses changed, except
DNS_ON_ETH0, which is from a probe on Docker's default bridge, where DNS is not on `lo`.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "no-third-party" / "classify.py"
_spec = importlib.util.spec_from_file_location("no3p_classify", _PATH)
classify_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = classify_module  # @dataclass looks its module up there
_spec.loader.exec_module(classify_module)

CONTROL_ADDR = "192.0.2.1.9"
CONTROL_NAME = "linkling-control-3991486b17817f60.invalid"

# --- recorded --------------------------------------------------------------------------------
GATEWAY_IGMP = "09:52:14.176671 eth0  M   IP 172.19.0.1 > 224.0.0.22: igmp v3 report, 1 group record(s)"
ARP = "09:52:14.858099 eth0  Out ARP, Request who-has 172.19.0.3 tell 172.19.0.3, length 28"
REPLY = (
    "09:52:17.017685 eth0  Out IP 172.19.0.3.8000 > 192.168.65.1.22851: Flags [S.], seq 1125222203, "
    "ack 1971639117, win 65160, length 0"
)
REQUEST = "09:52:17.017659 eth0  In  IP 192.168.65.1.22851 > 172.19.0.3.8000: Flags [S], seq 1971639116, win 65408, length 0"
HEALTHCHECK = [
    "09:52:16.548648 lo    In  IP 127.0.0.1.51142 > 127.0.0.1.8000: Flags [S], seq 1774548582, win 65495, length 0",
    "09:52:16.548658 lo    In  IP 127.0.0.1.8000 > 127.0.0.1.51142: Flags [S.], seq 3602259478, win 65483, length 0",
]
CONTROL_SYN = (
    "09:52:17.332453 eth0  Out IP 172.19.0.3.44200 > 192.0.2.1.9: Flags [S], seq 3648130256, "
    "win 64240, length 0"
)
CONTROL_DNS = [
    "09:52:17.360512 lo    In  IP 127.0.0.1.46482 > 127.0.0.11.42804: UDP, length 59",
    "E..W2.@.@.	............4.C.`.a..........!linkling-control-3991486b17817f60.invalid.....",
    "09:52:17.361835 lo    In  IP 127.0.0.11.53 > 127.0.0.1.46482: 56167 0/0/0 (59)",
    "E..W1.@.@.",
    "09:52:17.445136 lo    In  IP 127.0.0.11.53 > 127.0.0.1.46482: 35681 NXDomain 0/0/0 (59)",
]

CLEAN = [GATEWAY_IGMP, ARP, REQUEST, REPLY, *HEALTHCHECK, CONTROL_SYN, *CONTROL_DNS]

# --- leaks -----------------------------------------------------------------------------------
SYN_443 = "09:52:18.000001 eth0  Out IP 172.19.0.3.50000 > 93.184.215.14.443: Flags [S], seq 1, win 64240, length 0"
# The direction decides, not the port: a connection *to* someone else's port 8000 is a leak.
SYN_TO_REMOTE_8000 = "09:52:18.000002 eth0  Out IP 172.19.0.3.50001 > 93.184.215.14.8000: Flags [S], seq 1, win 64240, length 0"
DNS_ON_LO = [
    "09:52:18.000003 lo    In  IP 127.0.0.1.40000 > 127.0.0.11.42804: UDP, length 41",
    "E..E..@.@.	..........fonts.googleapis.com.....",
]
DNS_ON_ETH0 = "09:52:18.000004 eth0  Out IP 172.17.0.2.49274 > 192.168.65.7.53: 6580+ A? example.com. (29)"
IPV6_SYN = "09:52:18.000005 eth0  Out IP6 fd00::3.50002 > 2606:2800:21f:cb07::1.443: Flags [S], seq 1, win 64800, length 0"
UDP_OUT = "09:52:18.000006 eth0  Out IP 172.19.0.3.123 > 162.159.200.1.123: NTPv4, Client, length 48"


def run(lines, port="8000"):
    return classify_module.classify(lines, port, CONTROL_ADDR, CONTROL_NAME)


def test_a_recorded_clean_capture_is_clean_and_sees_both_controls_and_the_replies():
    violations, controls, replies, unparsed = run(CLEAN)
    assert violations == []
    assert unparsed == []
    assert all(controls.values()), controls
    assert replies == 1


@pytest.mark.parametrize(
    "leak",
    [[SYN_443], [SYN_TO_REMOTE_8000], DNS_ON_LO, [DNS_ON_ETH0], [IPV6_SYN], [UDP_OUT]],
    ids=["syn-443", "syn-to-remote-8000", "dns-on-lo", "dns-on-eth0", "ipv6-syn", "udp"],
)
def test_each_kind_of_sent_packet_is_a_violation_named_by_its_line(leak):
    violations, *_ = run(CLEAN + leak)
    assert [p.raw for p in violations] == [leak[0]]


def test_a_packet_received_from_outside_is_not_something_the_service_sent():
    unsolicited = "09:52:18.000007 eth0  In  IP 93.184.215.14.443 > 172.19.0.3.50000: Flags [R], seq 1, win 0, length 0"
    violations, *_ = run(CLEAN + [unsolicited])
    assert violations == []


def test_the_dns_control_is_excused_only_by_its_own_random_name():
    other_name = [CONTROL_DNS[0], CONTROL_DNS[1].replace("3991486b17817f60", "0000000000000000"), *CONTROL_DNS[2:]]
    violations, controls, _, _ = run([REPLY, CONTROL_SYN, *other_name])
    assert not controls["dns for " + CONTROL_NAME]
    assert CONTROL_DNS[0] in [p.raw for p in violations]


def test_the_tcp_control_is_excused_only_at_its_exact_address_and_port():
    near_miss = CONTROL_SYN.replace("192.0.2.1.9:", "192.0.2.1.10:")
    violations, controls, _, _ = run([REPLY, *CONTROL_DNS, near_miss])
    assert not controls["tcp to " + CONTROL_ADDR]
    assert [p.raw for p in violations] == [near_miss]


def main_exit(lines, capsys, port="8000"):
    stdin = sys.stdin
    sys.stdin = io.StringIO("\n".join(lines) + "\n")
    try:
        code = classify_module.main(
            ["--service", "api", "--service-port", port,
             "--control-addr", CONTROL_ADDR, "--control-name", CONTROL_NAME]
        )
    finally:
        sys.stdin = stdin
    return code, capsys.readouterr().out.strip().splitlines()[-1]


def test_exit_status_pass_fail_and_each_blind(capsys):
    assert main_exit(CLEAN, capsys) == (
        0, "clean: api sent only 1 reply packet(s) to the client, and both controls were seen"
    )

    code, last = main_exit(CLEAN + [SYN_443], capsys)
    assert (code, last) == (
        1, "fail: api sent 1 packet(s) to someone other than the client, the first IP to 93.184.215.14.443"
    )

    # An empty capture passes exactly as a clean one would, so it must be blind.
    code, last = main_exit([], capsys)
    assert code == 2 and last.startswith("blind: the api capture did not see its control")

    code, last = main_exit([REPLY, CONTROL_SYN], capsys)
    assert code == 2 and "dns for " + CONTROL_NAME in last

    code, last = main_exit([CONTROL_SYN, *CONTROL_DNS], capsys)
    assert code == 2 and last.startswith("blind: the api capture holds no reply from port 8000")

    # A header-less line after a packet is taken as that packet's bytes; before any packet it
    # cannot be, and the capture is not what the classifier expects.
    code, last = main_exit(["not a tcpdump line at all", *CLEAN], capsys)
    assert code == 2 and last.startswith("blind: 1 line(s) of the api capture could not be parsed")

    # A leak outranks a missing control: it was seen.
    code, last = main_exit([SYN_443], capsys)
    assert code == 1
