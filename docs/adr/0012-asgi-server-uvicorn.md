# ADR-0012 — uvicorn is the ASGI server, and the only new runtime dependency

- Status: Accepted
- Approver: claude
- Date: 2026-09-22

## Context

The brief fixes the stack at "Python 3.12, FastAPI, SQLite" (`raw/brief.md#L55`) and says
nothing about the server that runs the FastAPI application. `decision-policy.md` puts
"adding or removing a runtime dependency" in the row Claude decides **with an ADR**, so the
choice is recordable rather than free, even though it looks settled.

It is partly settled already, in a way worth stating: ADR-0004c reaches into
`uvicorn/config.py` to establish that the default access log writes the clicker's IP
address, and proposes that "the compose file runs the service with access logging off".
That Accepted ADR therefore *assumes* uvicorn without ever deciding it. An assumption that
load-bearing should not stay implicit — the next person to read ADR-0004 would have no way
to tell whether uvicorn was chosen or merely illustrated.

## Decision

**Proposed: uvicorn, declared as a runtime dependency of the `linkling` package, and run
with its access log off.**

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| **A. uvicorn** | One dependency; its defaults leak the clicker's address unless turned off (ADR-0004c) | Nothing — any ASGI server can replace it, since nothing imports it outside the entry point and one test | **R1** |
| B. hypercorn | Same shape, less exercised in this stack; HTTP/2 and HTTP/3, which a redirect does not need | Nothing | R1 |
| C. granian | Fewer eyes on it, a Rust build in the image | Nothing | R2 — a compiled dependency to keep building |
| D. Declare no server; leave it to the image (LL-005) | The package cannot be run or tested as shipped, and the one test that proves the redirect over a real socket would have nothing to run | The service being runnable from a checkout | R1 |

**Recommend A.** It is what ADR-0004c already read and assumed, and it is the most
exercised server in this stack. Nothing under `src/` imports it — the application is
plain ASGI — so it appears only in `pyproject.toml`, in `README.md`'s run command, and in
the tests: `tests/conftest.py` imports it for the `live_base_url` fixture, which
`tests/test_live_server_over_http.py` and `tests/test_live_server_concurrency.py` use to
run the app on a loopback port. D is rejected because it would leave the repository's own
tests unable to prove the HTTP half of LL-014.

**The access log is part of this decision, not a deployment detail.** Run without
`--no-access-log` (or an equivalent format carrying no `client_addr`), the service writes
the clicker's IP address to stdout, and Docker keeps stdout on disk — the privacy page
would be false on day one. `README.md` carries the flag in the command it documents; the
shipped compose file is LL-005's and must carry it too.

## Consequences

- `linkling` declares exactly two runtime dependencies: `fastapi` and `uvicorn`. Everything
  else in the service is the standard library, `sqlite3` included.
- Replacing the server later touches the entry point, the README and one test, and no
  application code — which is what makes this a recordable decision rather than a door.
- LL-005 inherits a stated requirement rather than a preference: whatever the compose file
  runs, it runs with the access log off.

**What would settle the only empirical question:** if the redirect path ever needs more
throughput than one uvicorn worker gives on the target machine, ADR-0005's own note about
measuring redirects per second is the measurement to take first — the server choice is
downstream of that number, not upstream of it.

## Decision record

Proposed, pending a stakeholder's acceptance through the manager. Written while building
LL-001, whose report names this file.

## Decision record

Accepted 2026-09-22 by `claude`, as the program manager `pm-2`, under the *adding or removing a runtime
dependency* row of `products/linkling/decision-policy.md`, which is Claude's with an ADR. The
access-log requirement this carries is not a preference: uvicorn's default access log writes the
clicker's IP address, which would break the privacy promise ADR-0004 states, so any compose file
or run command must pass `--no-access-log` or a format without `client_addr`. LL-005 owns honouring
that.
