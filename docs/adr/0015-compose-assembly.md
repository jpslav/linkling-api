# ADR-0015 — How the compose file is assembled: the site's source, the service's user, logging, and origins

- Status: Accepted
- Approver: pm-3
- Date: 2026-09-25

## Context

ADR-0007 settles the shape of the stack. The service is built from this repository, the
database sits on a bind mount at `./data:/data`, there is no reverse proxy inside the compose
file, backups are manual, and the public site is a static container in the same compose file.
It leaves four things open, and each one binds whoever deploys Linkling:

- where the site's image comes from, given that `linkling-web` is a separate, private
  repository;
- which user the service runs as, given that a bind mount's ownership is the host's;
- whether anything in the compose file logs a visitor's address;
- how the site and the service share a host, given that ADR-0001a gives the service the whole
  root namespace (`<host>/<name>`).

Two facts were measured before this was written:

- **Stock `nginx:1.27-alpine`, the site's base image, logs every visitor's address.** Requests
  to `/` and to a missing path logged `192.168.65.1 - - [...] "GET / HTTP/1.1" 200 …` in the
  access log, and the missing path also logged `open() "…/nope-probe-path" failed …, client:
  192.168.65.1` in the error log. With `access_log off; log_not_found off;` mounted into
  `conf.d/`, those two requests logged nothing, but that was not enough. An oversized POST
  (413) and a directory with no index (403) still logged `client: 192.168.65.1` at `error`,
  and a docroot entry that is not a regular file logged it at `crit`. Adding
  `error_log /dev/null;` to the same file stopped all of them, and nginx's startup output
  still appeared. (Probes on 2026-09-23, quoted on
  [api #8](https://github.com/jpslav/linkling-api/pull/8) and in its review rounds.)
- **A compose file whose build context is missing fails at build time and names the path:**
  `unable to prepare context: path "…" not found`, exit 1.

## Decision

**Proposed.**

### i. The site is built from a sibling checkout

| Option | Costs | Blind failure | Recurring |
|---|---|---|---|
| **A. `build: ${LINKLING_WEB_DIR:-../linkling-web}`, a sibling checkout** | Two clones side by side, the layout ADR-0008 already assumes for its privacy check | None: a missing sibling is exit 1 with the path named | R1 |
| B. A git-URL build context | A private repository needs a credential forwarded into BuildKit, and the build follows a moving `main` | Auth failures read as build failures | R2 |
| C. A published image in a registry | A publish job and pull credentials; ADR-0007a chose "no registry to publish to" | A stale tag silently serves old pages | R3 |
| D. A git submodule | `git clone` without `--recurse-submodules` leaves an empty context | Dockerfile-not-found | R2 |

**A.**

### ii. The service runs as uid 10001, and the entrypoint makes the bind mount writable for it

On Linux, a host directory that Docker creates for a bind mount is owned by root (INFERRED
from Docker's bind-mount documentation), so a non-root service cannot create its database
there. Docker Desktop's file sharing hides this on a Mac.

| Option | Costs | Recurring |
|---|---|---|
| A. Run as root | A root-owned database on the host, and root inside the container | R1 |
| **B. Start as root, `chown` `/data`, and any `linkling.db*` file in it, to uid 10001 wherever uid 10001 does not already own them, then drop to that uid with `setpriv` before starting uvicorn** | One short entrypoint. On Linux, the host's `./data` ends up owned by uid 10001 | R1 |
| C. `user:` from a variable, plus a manual `mkdir`/`chown` step | Breaks "a single `docker compose up`" on Linux | R4, for every deployer |

**B.** CI runs the stack on Linux, which is where the question actually arises.

### iii. Nothing sits in front of the service, and the site's nginx logs nothing about visitors

The compose file mounts `deploy/nginx-privacy.conf` (`access_log off; log_not_found off;
error_log /dev/null;`) into the site container. The service runs with `--no-access-log` (ADR-0012). A deployer who puts a
TLS proxy in front is told, in the README, to turn that proxy's access log off too: a proxy
outside the compose file is outside what this product can promise. The better long-term home
for the nginx setting is `linkling-web`'s own image. The mount stays harmless once that lands.

### iv. The site and the service have separate origins

ADR-0001a puts link names at the root of the service's host, and ADR-0007a rules out a proxy
inside the compose file that could split one host between two containers. So the compose file
publishes two ports, and a deployer points a hostname at each: the service on
`${LINKLING_PORT:-8000}` and the site on `${LINKLING_WEB_PORT:-8080}`. Serving the site at the
service's `/` is not an option this leaves open.

### Deploy-time names

| Name | Default | |
|---|---|---|
| `LINKLING_API_KEY` | none — compose refuses to start and names it | the team key (ADR-0006) |
| `LINKLING_DATA_DIR` | `./data` | the host directory bind-mounted at `/data` |
| `LINKLING_PORT` | `8000` | the service's host port |
| `LINKLING_WEB_PORT` | `8080` | the site's host port |
| `LINKLING_WEB_DIR` | `../linkling-web` | the site's build context |

Inside the container, `LINKLING_DB` is fixed at `/data/linkling.db`.

`LINKLING_PUBLIC_URL` (ADR-0007a) is **not** passed in yet, because nothing in the service reads
it. Each deployment still sets its own. Whichever item first reads it adds it here as required,
which is a one-line compose change and does not reopen this record.

## Consequences

- A clean checkout of `linkling-api` means two clones side by side. CI can build only the
  service, because a workflow's token reaches only its own repository (ADR-0008). *(Added
  2026-09-24, LL-028: CI also builds a stand-in for the site, `tests/fixtures/standin-site`, to
  run the site half of the no-third-party check. It is not `linkling-web`, so the real site
  is still never built in CI, and nginx and `deploy/nginx-privacy.conf` are not exercised
  there.)*
- The database is `./data/linkling.db` on the host, owned on Linux by uid 10001. `docker compose down -v`
  does not touch it, because it is not a volume. `rm -rf data` or `git clean -fdx` does delete
  it.
- Neither container logs a visitor's address. A proxy in front of them is the deployer's to
  configure.

## Decision record

Accepted 2026-09-25 by `pm-3`, the program manager, under the 2026-09-22 ruling in `company/DECISIONS.md` of the program repo ("What reaches the owner is user-visible impact, not cost to undo"): where the site's image is built from, which uid the service runs as, what the site's nginx logs and how the two origins share a host are not something a user of Linkling perceives. Proposed since 2026-09-23. Checked against `main` at `42444f5` before accepting: `compose.yaml`'s `web` service builds `${LINKLING_WEB_DIR:-../linkling-web}` and mounts `deploy/nginx-privacy.conf` (`access_log off; log_not_found off; error_log /dev/null;`), and `deploy/entrypoint.sh` `chown`s to uid 10001 and then `exec setpriv`s to it. One name the deploy-time table above predates: `LINKLING_BIND_ADDR`, which ADR-0017 added to both `ports:` lines of `compose.yaml`. Recorded by the `w-LL-030` session (LL-031) in jpslav/linkling-api#20.
