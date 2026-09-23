#!/bin/sh
# Runs tcpdump in the background and keeps the container alive after it stops, so the check can
# stop the capture with SIGINT (which is what makes tcpdump flush and report its kernel drops)
# and still read the capture out of this container afterwards.
#   $1  the tcpdump filter expression; empty means every packet. It is non-empty only when
#       LINKLING_NO3P_CAPTURE_FILTER is set, and the check then never reports `pass`.
set -eu
mkdir -p /cap
# -Z root: tcpdump would otherwise drop to its own user, which cannot write /cap.
# shellcheck disable=SC2086
tcpdump -i any -nn -U -Z root -w /cap/capture.pcap ${1:-} 2>/cap/tcpdump.err &
echo $! >/cap/tcpdump.pid
wait || true
exec sleep infinity
