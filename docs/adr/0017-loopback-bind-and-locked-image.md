# ADR-0017 — Loopback bind by default, and a locked, digest-pinned image

- Status: Accepted
- Approver: pm-3
- Date: 2026-09-25

## Context

Two findings from `w-LL-005` (report item 5), filed as LL-021 and LL-022. `compose.yaml`
published both the service's and the site's ports with no bind address, which Docker
resolves to `0.0.0.0` and `[::]`: reachable from off the host by construction, even around
a TLS proxy a deployer put in front, and — measured by `w-LL-018` — even when a host process
already held the port, in which case compose still published `0.0.0.0:<port>` and the
squatter answered. Separately, the image built from a floating `python:3.12-slim` tag and an
unpinned `pip install .`, so a green CI run said nothing certain about what a deployer's
build would resolve. `products/linkling/decision-policy.md` puts "what fronts the service"
and "Docker base images and how they are pinned" both on Claude's side, each with an ADR.

## Decision

**Proposed.**

### i. The bind default

| Option | Costs | Blind failure | Recurring |
|---|---|---|---|
| A. Keep `0.0.0.0` (status quo) | The finding this item exists to fix: a proxy in front can be bypassed on the raw port, carrying the team key in cleartext | None — it is the default, so nothing signals it | R1, until it is not |
| **B. Bind `127.0.0.1` by default; `LINKLING_BIND_ADDR` widens it** | One new variable; a deployer who wants no proxy at all must set it once, and the README says so | None: an unset variable is loopback, a set one is whatever was set, both legible in `docker compose port` | R1 |
| C. Publish no port at all by default, require a compose override | More conservative than B, but breaks "one `docker compose up`" (ADR-0007a) for every deployer, including one who wants no proxy | A deployer who does not read the override docs has no service at all, which looks like a crash, not a choice | R1, but a worse first-run experience |
| D. A reverse-proxy container inside the compose file | Already foreclosed — ADR-0007a: "No reverse proxy inside the compose file" | — | — |

**B.** One shared variable, `LINKLING_BIND_ADDR` (default `127.0.0.1`), used on both the
service's and the site's published-port lines, rather than two independent variables. The
README already tells a deployer to front both origins with a proxy and to turn both proxies'
access logging off (`README.md:61-70`), so the two ports have the same exposure story, and a
deployer opting out of the safe default almost certainly wants it off for both. A deployer
who genuinely wants different postures per port can still get there by editing one line —
a real but small loss, accepted rather than hidden.

### ii. The lock file and the base image pin

| Option (lock shape) | Costs | Recurring |
|---|---|---|
| A. No lock (status quo) | A green CI run proves nothing about what a deployer's build resolves | R1, until a transitive dependency pulls in `websockets` or `wsproto` and silently reopens the leak ADR-0012 closed |
| **B. `uv pip compile` → hashed `requirements.lock.txt`; plain `pip install` in the Dockerfile** | A file to regenerate when `pyproject.toml` changes | R4 — recurring, named below |
| C. `uv`'s project-mode `uv.lock` (`uv sync` in the image) | A new runtime toolchain in the image, a `.venv`, a different `CMD` — more than this item needs, and against `linkling-web`'s own ADR-0002 convention ("no build toolchain unless you can say why one is needed") | R4, same as B, plus a bigger image-build surface to keep working |

**B.** `linkling-web` already depends on `uv` (it has a tracked `uv.lock`), so the tool is not
new to this product, but its lock file is project-mode and that repo has no runtime
dependencies to install into an image — not a precedent for the file *shape*. Pip-compile
mode keeps the Dockerfile's existing `pip install` shape (no `uv` binary ships in the final
image) while getting the same reproducibility, with SHA-256 hashes on every pinned wheel so a
tampered or substituted download is refused, not just a version drift.

| Option (base image) | Costs | Recurring |
|---|---|---|
| A. Tag only (`python:3.12-slim`) | LL-022's "Done when" requires a digest; a tag can move under a rebuild | — |
| **B. Digest, tag kept alongside for legibility** | Someone has to re-measure and re-pin it periodically | R4 |

**B.** `linkling-web`'s ADR-0002 chose tag-only for its `nginx:1.27-alpine` image and said to
"revisit if a dependency-update bot is added later" — that image copies in two
static HTML pages with no application dependencies. This image installs a real Python
application with a real dependency tree, so the two repos are choosing differently on
purpose: reproducibility matters more here, and it is worth saying explicitly rather than
leaving the divergence to look like an oversight.

## Consequences

- An unmodified `docker compose up` now publishes neither port off the host; a deployer who
  wants that (no proxy, e.g. a LAN-only box) sets `LINKLING_BIND_ADDR=0.0.0.0` once, documented
  in the README.
- The image's Python dependencies and base image are both pinned; a floating range or tag no
  longer silently changes what a deployer's build resolves to.
- `requirements.lock.txt` and the base image digest are both recurring maintenance: someone
  (or something) has to re-run `uv pip compile` and re-measure the digest when a dependency
  needs to move. Named as a concrete follow-up in the report that carries this ADR, with a
  mechanism rather than a person as the owner.
- The apt-installed `sqlite3` package stays unpinned — out of scope for this ADR; LL-022's
  "Done when" names the Python dependencies and the base image, not apt packages.

## Decision record

Accepted 2026-09-25 by `pm-3`, the program manager, under the 2026-09-22 ruling in `company/DECISIONS.md` of the program repo ("What reaches the owner is user-visible impact, not cost to undo"): which interface the ports bind to by default, and how the image's dependencies and base image are pinned, are not something a user of Linkling perceives. Proposed since 2026-09-23. Checked against `main` at `42444f5` before accepting: `compose.yaml` publishes both ports as `"${LINKLING_BIND_ADDR:-127.0.0.1}:…"`, and the `Dockerfile` is `FROM python:3.12-slim@sha256:2f17fc04…` and installs `requirements.lock.txt` and `requirements-build.lock.txt` with `pip install --require-hashes`. Two things in it have moved on. The README passage cited in i as `README.md:61-70` is now the environment-variable table; the proxy access-log instruction it meant is the README paragraph that ends "Otherwise the privacy promise stops being true at your front door." The recurring re-lock and re-pin the last consequence names as a follow-up is what ADR-0018 does with Dependabot. Recorded by the `w-LL-030` session (LL-031) in jpslav/linkling-api#20.
