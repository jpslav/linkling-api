"""Judges one container's packet capture for scripts/no-third-party-check.sh (LL-018, ADR-0009).

Reads the text `tcpdump -nn -A -r <capture>` prints for a capture taken with `-i any`, on
stdin: a header line per packet, then that packet's bytes as ASCII. The bytes are needed
because a DNS query on `lo` arrives rewritten to a port tcpdump does not decode as DNS, so its
header line names no name. Each header carries the interface and the direction the kernel recorded:
`Out` is a packet this network namespace sent. `In`, `M` (multicast) and `B` (broadcast) are
packets it received. What the service *sends* is the question, so only `Out` is judged, plus
every packet on `lo`, where both ends are the container itself.

A sent packet is allowed only when it is one of:

- TCP from the service port on a non-loopback interface, other than a SYN without ACK: a reply
  to a client. A SYN from that port would be the service opening a connection of its own.
- TCP on `lo` with the service port at either end: the compose healthcheck, which runs inside
  the container and never leaves it.
- ARP, or anything addressed to link-scope multicast (224.0.0.0/24, ff02::/16): interface
  housekeeping that no router forwards off the bridge.
- One of this run's positive controls (below).

Everything else is a violation. That includes every DNS query, because resolving a name is the
first step of contacting it, and Docker's resolver passes a query for an outside name on to
the host's own resolvers (https://docs.docker.com/engine/network/:
"embedded DNS server forwards external DNS lookups to the DNS servers configured on the host").

The positive controls are planted by the check from inside the namespace, on every run: a TCP
connection to `--control-addr` and a lookup of the random `--control-name`. Both must appear,
and so must at least one reply from the service port, or the capture is not proven to be of
this namespace doing this run's work. They are excused narrowly: a packet to that exact
address and port; a packet whose bytes carry that random label; and a packet on `lo` to or
from the source endpoint (never port 53) of a query carrying it. That last is the same query
and its replies: Docker's resolver rewrites the query's port 53 to one of its own (measured:
`127.0.0.1.34118 > 127.0.0.11.57727: UDP`), and tcpdump names no name in those headers.

Prints one line per finding and ends with a summary line. Exit status: 0 clean, 1 at least one
violation (a violation seen is an observation even if a control went missing), 2 blind: a
control or the service's replies were not seen, or a line could not be parsed.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from dataclasses import dataclass

# "09:33:52.702402 eth0  Out IP 172.17.0.2.40824 > 192.0.2.1.9: Flags [S], ..."
_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{6} ")
_LINE = re.compile(
    r"^\S+\s+(?P<iface>\S+)\s+(?P<dir>In|Out|M|B|P|U)\s+(?P<proto>\S+?),?\s+(?P<rest>.*)$"
)
_LINK_SCOPE = (ipaddress.ip_network("224.0.0.0/24"), ipaddress.ip_network("ff02::/16"))

#: How many violations a failing run names; the count is always given in full.
SHOWN = 5


@dataclass
class Packet:
    raw: str
    iface: str
    direction: str
    proto: str  # "IP", "IP6", "ARP", or whatever else tcpdump printed there
    src: str = ""
    dst: str = ""
    header: str = ""  # what the header line says after "src > dst: "
    payload: str = ""  # the header's text, then the `-A` dump of the packet's bytes

    @property
    def tcp(self) -> bool:
        # From the header line only: the dump is the packet's bytes, which anyone can fill.
        return self.proto in ("IP", "IP6") and self.header.startswith("Flags [")

    @property
    def opens_connection(self) -> bool:
        # SYN without ACK, whatever else is set: an ECN SYN prints as `Flags [SEW]`.
        flags = self.header[len("Flags ["):].split("]", 1)[0] if self.tcp else ""
        return "S" in flags and "." not in flags

    @staticmethod
    def port(endpoint: str) -> str:
        return endpoint.rsplit(".", 1)[1] if "." in endpoint else ""

    @staticmethod
    def address(endpoint: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        # An endpoint is "<addr>.<port>" for TCP and UDP, and a bare address otherwise.
        for candidate in (endpoint, endpoint.rsplit(".", 1)[0]):
            try:
                return ipaddress.ip_address(candidate)
            except ValueError:
                continue
        return None


def parse(line: str) -> Packet | None:
    m = _LINE.match(line)
    if not m:
        return None
    packet = Packet(line, m["iface"], m["dir"], m["proto"])
    if packet.proto in ("IP", "IP6"):
        src, sep, rest = m["rest"].partition(" > ")
        dst, sep2, payload = rest.partition(": ")
        if not (sep and sep2):
            return None
        packet.src, packet.dst, packet.header, packet.payload = src, dst, payload, payload
    return packet


def classify(lines, service_port: str, control_addr: str, control_name: str):
    """Returns (violations, controls_seen, replies_seen, unparsed)."""
    violations: list[Packet] = []
    unparsed: list[str] = []
    control_syn = control_dns = False
    replies = 0
    control_query_sources: set[str] = set()

    packets: list[Packet] = []
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if not _TIMESTAMP.match(line):
            # `tcpdump -A` prints each packet's bytes, as ASCII, on the lines after its header.
            if packets:
                packets[-1].payload += "\n" + line
            else:
                unparsed.append(line)
            continue
        packet = parse(line)
        if packet is None:
            unparsed.append(line)
            continue
        packets.append(packet)

    # Learn the control queries' source endpoints first, so that the packets on lo that carry
    # no name (the rewritten copy, the replies) can be excused wherever they fall.
    # Only a query's source counts: the resolver's replies carry the name too, and learning
    # their source (127.0.0.11.53) would excuse its replies to every other query.
    for p in packets:
        if control_name in p.payload and Packet.port(p.src) != "53":
            control_query_sources.add(p.src)

    for p in packets:
        loopback = p.iface == "lo"
        if not loopback and p.direction != "Out":
            continue
        if p.proto.startswith("ARP"):
            continue
        if p.proto not in ("IP", "IP6"):
            violations.append(p)
            continue
        if p.dst == control_addr:
            control_syn = True
            continue
        if control_name in p.payload:
            control_dns = True
            continue
        if loopback and (p.src in control_query_sources or p.dst in control_query_sources):
            continue
        if p.tcp:
            if loopback and service_port in (Packet.port(p.src), Packet.port(p.dst)):
                continue
            # A reply leaves from the service port. A bare SYN from it would be the service
            # opening a connection of its own from its listening port, which is not a reply.
            if not loopback and Packet.port(p.src) == service_port and not p.opens_connection:
                replies += 1
                continue
        dst = Packet.address(p.dst)
        if dst is not None and any(dst in net for net in _LINK_SCOPE if dst.version == net.version):
            continue
        violations.append(p)

    controls = {"tcp to " + control_addr: control_syn, "dns for " + control_name: control_dns}
    return violations, controls, replies, unparsed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--service", required=True, help="name used in messages, e.g. api")
    ap.add_argument("--service-port", required=True)
    ap.add_argument("--control-addr", required=True, help='as tcpdump prints it, e.g. "192.0.2.1.9"')
    ap.add_argument("--control-name", required=True)
    ap.add_argument(
        "--target-host",
        help="the host of the link the check created; a sent packet naming it is a lookup of it",
    )
    args = ap.parse_args(argv)

    violations, controls, replies, unparsed = classify(
        sys.stdin, args.service_port, args.control_addr, args.control_name
    )
    for p in violations[:SHOWN]:
        print(f"{args.service}: sent: {p.raw}")
    if violations:
        more = f" ({len(violations) - SHOWN} more not shown)" if len(violations) > SHOWN else ""
        first = violations[0]
        to = f"{first.proto} to {first.dst}" if first.dst else first.proto
        looked_up = args.target_host and any(args.target_host in p.payload for p in violations)
        target = f", including a lookup of the link's target host {args.target_host}" if looked_up else ""
        print(
            f"fail: {args.service} sent {len(violations)} packet(s) to someone other than the "
            f"client, the first {to}{more}{target}"
        )
        return 1
    for line in unparsed[:SHOWN]:
        print(f"{args.service}: could not parse: {line}")
    if unparsed:
        print(f"blind: {len(unparsed)} line(s) of the {args.service} capture could not be parsed")
        return 2
    missing = [name for name, seen in controls.items() if not seen]
    if missing:
        print(f"blind: the {args.service} capture did not see its control ({', '.join(missing)})")
        return 2
    if not replies:
        print(f"blind: the {args.service} capture holds no reply from port {args.service_port}, so it did not see this run's requests")
        return 2
    print(f"clean: {args.service} sent only {replies} reply packet(s) to the client, and both controls were seen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
