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
expiry), following it, and deleting it. Counting, the stats page and the CLI are separate
pieces of work and are not here yet.

| | |
|---|---|
| `POST /-/api/links` | `{"url": …, "name": …?, "created_by": …?, "expires": …?}` → `201 {"name", "url"}`. Needs the team key. |
| `DELETE /-/api/links/<name>` | `204`. Needs the team key. A deleted name stays reserved forever. |
| `GET\|HEAD /<name>` | `302` to the long URL with `Cache-Control: no-store`. No credential, no cookie. |

Unknown names answer `404`; deleted or expired ones, `410`; and every response the
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
echo 'LINKLING_API_KEY=a-long-random-team-key' > .env    # git-ignored; never commit it
docker compose up -d
```

| Name | Default | |
|---|---|---|
| `LINKLING_API_KEY` | none: compose refuses to start and names it | the team key |
| `LINKLING_DATA_DIR` | `./data` | where the database lives on the host |
| `LINKLING_PORT` | `8000` | the service's host port: short links and the API |
| `LINKLING_WEB_PORT` | `8080` | the public site's host port |
| `LINKLING_WEB_DIR` | `../linkling-web` | the site's checkout |

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
and most proxies log every client's address by default. Turn its access log off, or the
privacy promise stops being true at your front door.

`scripts/compose-smoke.sh` checks a stack started from nothing. It confirms that the service
answers, that a link survives `docker compose down` and `up`, that the backup below is sound,
and that the logs hold no client address. It uses its own project, port and data directory,
so it does not touch a stack you already run. CI runs it on every pull request.

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
container because the service opens the database in WAL mode, and it is the command the
CLI's `linkling backup` verb will wrap.

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
