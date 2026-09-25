# linkling-api

The Linkling service: the short-link API, redirects, click counts, the stats page and the CLI.

Part of **Linkling**, a small self-hosted link shortener built by the Tinyworks program.
Every decision with reach is written down first, in `docs/adr/`, and the code is built to it.

**Durability is the deployer's.** The link database is a single SQLite file, and nothing here
backs it up for you. Backups are manual: somebody has to run the command in
[Back it up](#back-it-up) and copy the file off the machine, and nothing reminds them or
notices when they stop — see `docs/adr/0007-compose-topology.md`.

## What exists today

Creating a link (with a name you choose or one the service invents, optionally given an
expiry), following it, deleting it, and a page that lists every link with its counts. Every
redirect adds one to that link's count for the current UTC day, and that per-link per-day
number is all the service's tables hold about a click (`docs/adr/0004`, `0014`); deleting a
link deletes its counts. The CLI is a separate piece of work and is not here yet.

| | |
|---|---|
| `POST /-/api/links` | `{"url": …, "name": …?, "created_by": …?, "expires": …?}` → `201 {"name", "url"}`. Needs the team key. |
| `DELETE /-/api/links/<name>` | `204`. Needs the team key. A deleted name stays reserved forever. |
| `GET\|HEAD /<name>` | `302` to the long URL with `Cache-Control: no-store`. No credential, no cookie. |
| `GET /-/api/links/<name>/stats` | `200 {"days": {"YYYY-MM-DD": count}}`: that link's count for each UTC day it was followed, and no row for a day nobody followed it. Needs the team key. `404` never existed, `410` deleted; an expired link answers `200` with the counts it kept. Reading it does not count as a follow. |
| `GET /-/stats` | A plain HTML page: every link that has not been deleted, and its count for each UTC day. Needs the team key over HTTP Basic. |

`/-/stats` is opened in a browser, so it takes HTTP Basic rather than the Bearer header the
API uses (`docs/adr/0006`): any user name without a colon in it, and the team key as the password. It lists every
link that has not been deleted, by name, with its target and its day-by-day counts, newest
day first. A link that has expired is listed and marked, and keeps its counts; a deleted
link is not listed, because its target and its counts went when it was deleted. Opening
the page does not count as a follow.

Following an unknown name answers `404`, and a deleted or expired one `410`; and every response the
application produces carries `Cache-Control: no-store` — a 500 from the framework's own
error handler is the one exception, and 500 is not a cacheable status. `expires` is an
ISO-8601 UTC timestamp strictly in the future, shaped like `2026-01-01T00:00:00Z`
(`docs/adr/0013`); omitted, a link never expires. The decisions are in `docs/adr/0001`,
`0003`, `0005`, `0006` and `0013`.

## Run it with Docker

The whole stack is the service and the public site, from `docker compose up`
(`docs/adr/0007-compose-topology.md`, `docs/adr/0015-compose-assembly.md`). The site is built
from a checkout of `linkling-web` **beside** this one, so a clean checkout means two clones:

```bash
git clone git@github.com:jpslav/linkling-api.git
git clone git@github.com:jpslav/linkling-web.git
cd linkling-api
echo "LINKLING_API_KEY='a-long-random-team-key'" > .env  # git-ignored; never commit it
docker compose up -d
```

| Name | Default | |
|---|---|---|
| `LINKLING_API_KEY` | none: compose refuses to start and names it | the team key |
| `LINKLING_DATA_DIR` | `./data` | where the database lives on the host |
| `LINKLING_PORT` | `8000` | the service's host port: short links and the API |
| `LINKLING_WEB_PORT` | `8080` | the public site's host port |
| `LINKLING_WEB_DIR` | `../linkling-web` | the site's checkout |
| `LINKLING_BIND_ADDR` | `127.0.0.1` | which interface both ports bind to |

Each can go in the shell's environment or in `.env`. Inside the container the database is
always `/data/linkling.db`. **`LINKLING_PUBLIC_URL`** is the host every short link is
printed with (`https://<host>/<name>`). Each deployment sets its own, and the product names
none. Nothing in the service reads it yet, so the compose file does not ask for it.

**The service and the site are two origins.** Short links own the root of their host
(`docs/adr/0001-short-url-shape.md`), so the site cannot share it: point one hostname at
`LINKLING_PORT` and another at `LINKLING_WEB_PORT`.

**Whatever you put in front of it must not log addresses either.** Neither container logs a
visitor's address — the service runs with `--no-access-log` and the site with nginx's logging
off (`deploy/nginx-privacy.conf`). A TLS proxy you add in front is outside the compose file,
and some proxies log every client's address by default. nginx does, which is why the site's
container needs that file. Turn your proxy's access log off, and keep client addresses out
of its error log too. Otherwise the privacy promise stops being true at your front door.

**Both ports bind to `127.0.0.1` by default, so nothing is reachable from off this host until
you opt in** (`docs/adr/0017`). This is deliberate: a TLS proxy in front only protects you if
the raw port cannot also be reached directly, and the raw port carries the team key
(`LINKLING_API_KEY`) in cleartext. If you are not putting a proxy in front — a LAN-only box,
say — set `LINKLING_BIND_ADDR=0.0.0.0` (or a specific interface's address) to publish both
ports everywhere.

`scripts/compose-smoke.sh` checks a stack started from nothing. It confirms that the service
answers, that a link survives `docker compose down` and `up`, that the backup below is sound,
and that the logs hold no client address. It uses its own project, port and data directory,
so it does not touch a stack you already run. CI runs it on every pull request.

`scripts/no-third-party-check.sh` shows that the stack sends nothing to anyone but the client
(`docs/adr/0009-third-party-services.md`). It captures every packet the service and the site
send, from before either one starts, while it creates a link, follows it, opens the stats page
and reads the link's counts, each with and without the team key, deletes the link, reads its
counts again, and loads the site. Anything but a reply to
its own requests fails the run, and so does any DNS lookup. Each run also plants a connection
and a lookup of its own, and a capture that misses them is reported blind rather than clean.
It also fetches the site's pages and every stylesheet they pull in, and fails on any
absolute URL, `<script>` or inline event handler in what they serve. The service's own stats
page prints each link's target as text, so it is read as HTML instead
(`scripts/no-third-party/loads.py`, whose docstring says what it flags and what it does not
model): it fails on markup that names another origin or runs script (a `<script>`, an inline
event handler, another origin's URL in a `src`, in an `href` other than a link's, or in CSS),
not on a target shown as text.
A request that gets no answer within 10 seconds (`LINKLING_SMOKE_MAX_TIME` and
`LINKLING_NO3P_MAX_TIME` change that) ends either script as blind, naming what it was asking
for, rather than waiting for CI's own timeout.
It cannot see routes it does not exercise, anything after the run ends, what a browser does
with the pages, what the host does outside the containers, or a proxy you put in front. CI
runs it with `--api-only`, because CI cannot fetch `linkling-web`, and runs it again with a
deliberate leak to show that it goes red, and again with a stats page that names a third-party
stylesheet. A second CI job runs the whole check, site included, against a small stand-in for the
site (`tests/fixtures/standin-site`, named with `LINKLING_WEB_DIR`), and then against four copies of
it that each carry one defect the check must catch (`scripts/no-third-party-standin.sh`): a
stylesheet on another origin (`fail`), a stylesheet reply that stops short or never ends
(`blind`), and a site that never listens (`blind`, because the `web` healthcheck below keeps
`docker compose up --wait` from returning). That shows the crawl runs, stops on its time bound and
goes red. It says nothing about what `linkling-web` serves, and the stand-in is not nginx, so
`deploy/nginx-privacy.conf` is not exercised either. Run it with the real site from a checkout that
has `linkling-web` beside it. The first line of each run says which site it built and which defect,
if any, was put in it (`site fixture none` for a real run, `site fixture cut-short-reply` under the
wrapper), and separately which compose overlay `--mutate` layered in (`compose mutation none`).

`docker compose up -d --wait` returns only once both services are answering: each has a
healthcheck. `web`'s asks the site for `/` on port 80 with `wget` if its image has one (nginx's
does) and `python3` if not (the stand-in's does), so an image with neither reports unhealthy
instead of ready.

### Keeping the pins fresh

The image's Python dependencies (`requirements.lock.txt` and `requirements-build.lock.txt`) and its
base image (`python:3.12-slim`, pinned by digest in three Dockerfiles: the service's, the
observer that `scripts/no-third-party-check.sh` builds, and the stand-in site's) go stale on their
own: a security release reaches no deployer until someone re-locks and re-pins. Dependabot does
that (`.github/dependabot.yml`, `docs/adr/0018-dependabot-refreshes-the-locks-and-digests.md`).
Every Monday it opens at most two pull requests, each only when something moved: one that
re-resolves both lock files together, and one that moves the three digests together. CI runs on
them like on any pull request, and the image build in the `compose` and `no-third-party` jobs is
what runs the websockets/wsproto guard (`scripts/no-forbidden-imports-check.sh`) against the new
lock. A green one is merged; a red one is the exception to look at. Nothing is refreshed by hand.

Two things make that work, and tests fail when either breaks: each lock has a `.in` file beside
it (`requirements.lock.in`, `requirements-build.lock.in`) saying what `pyproject.toml` says,
which is what makes Dependabot re-resolve the whole set instead of bumping one pinned line at a
time, and every Dockerfile is listed in the config. Dependabot is held to the `3.12-slim` tag: it
refreshes that tag's digest and never proposes another Python. To re-lock by hand, use the command
at the top of each lock file.

### Where the database lives

`./data/linkling.db` on the host (or under `LINKLING_DATA_DIR`), through a bind mount rather
than a Docker volume. On Linux the directory ends up owned by uid 10001, the service's own
user inside the container.

- `docker compose down` and `docker compose down -v` both leave it alone. It is not a volume.
- **`rm -rf data` or `git clean -fdx` deletes it.** `data/` is git-ignored, and `git clean -x`
  removes ignored files too. For anything you care about, set `LINKLING_DATA_DIR` to a
  directory outside the checkout.

### Back it up

With the stack running:

```bash
docker compose exec -u linkling api \
  sqlite3 /data/linkling.db ".backup '/data/backup-$(date -u +%Y%m%dT%H%M%SZ).db'"
```

The backup lands next to the database, in `./data/`. **Then copy it off this machine**: a
backup on the same disk goes wherever the disk goes. Run it before every upgrade, and as often
as you can bear to lose the links made since the last one. The command runs inside the
container. That needs no `sqlite3` on the host, and it reads the database on the same machine
the service writes it on. That matters because the service opens the database in WAL mode,
and WAL requires every reader to be on one host (<https://sqlite.org/wal.html>). Whether a
host-side read through Docker Desktop's file sharing would be safe is untested. This is the
command the CLI's `linkling backup` verb will wrap.

### Upgrade

Back up first. Then pull both checkouts and rebuild: a plain `docker compose up -d` reuses
the images it has already built.

```bash
git pull && git -C ../linkling-web pull
docker compose up -d --build
```

### Restore

On Linux, `data/` belongs to uid 10001, so the copy and the `rm` need `sudo`. The service
takes ownership of the restored file when it starts.

```bash
docker compose down
cp /path/to/backup-….db data/linkling.db
rm -f data/linkling.db-wal data/linkling.db-shm
docker compose up -d
```

## Run it locally

Two environment variables, both required and neither defaulted: the service refuses to
start without them rather than invent a key or put the database somewhere you would not
look for it.

Python 3.12 (`uv python install 3.12` if you have `uv` and no 3.12 on `PATH`):

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[test]"

export LINKLING_API_KEY="a-long-random-team-key"
export LINKLING_DB="$PWD/data/linkling.db"
mkdir -p "$(dirname "$LINKLING_DB")"

.venv/bin/uvicorn --factory linkling.server.app:create_app \
  --host 127.0.0.1 --port 8000 --no-access-log
```

`--no-access-log` is not tidiness. uvicorn's access log writes the client's IP address for
every request, and Docker keeps stdout on disk — so a service left on its defaults *keeps
IP addresses* while the privacy page says it does not (`docs/adr/0004-click-record-contents.md`).

Then, in another shell:

```bash
curl -sf -X POST http://127.0.0.1:8000/-/api/links \
  -H "Authorization: Bearer $LINKLING_API_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/a/very/long/tracking/url","name":"q3-plan"}'

curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:8000/q3-plan
curl -sf -u ":$LINKLING_API_KEY" http://127.0.0.1:8000/-/stats | grep q3-plan   # or open it in a browser
curl -sf -X DELETE http://127.0.0.1:8000/-/api/links/q3-plan \
  -H "Authorization: Bearer $LINKLING_API_KEY"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/q3-plan   # 410
```

**To check the deletion in a real browser** — which is the half of LL-014 that `curl`
cannot do — open `http://127.0.0.1:8000/q3-plan` in a browser so it caches whatever it is
given, delete the link with the `DELETE` above, and open the same URL again. It must not
reach the long URL a second time.

## Tests

```bash
.venv/bin/python -m pytest -q
```

They start the app themselves, including one that runs it under uvicorn on a port the
kernel picks, so nothing needs to be running first and no port is taken from anyone else.
