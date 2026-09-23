# Sourced by scripts/compose-smoke.sh and scripts/no-third-party-check.sh (LL-021, carried
# from w-LL-018): compose publishes a port from nothing, even one a host process already
# holds, and a script that then tests that squatter would describe it as the service. So
# refuse before docker is touched at all.
#
# port_free <port>: 0 (true) free, 1 taken, 2 could not tell (sets PORT_CHECK_MSG). Free only
# on an actively refused connect -- anything else (no /dev/tcp support, a sandboxed connect, a
# malformed argument, a timeout) is "could not tell", because a check that reads "never
# looked" the same as "looked and it was free" is worse than no check. Call only inside an
# `if`, never as a plain statement, so its non-zero returns are never subject to `set -e`.
port_free() {
    local out
    if out="$( (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>&1 )"; then
        return 1
    fi
    case "$out" in
        *"Connection refused"*) return 0 ;;
        *) PORT_CHECK_MSG="$out"; return 2 ;;
    esac
}
