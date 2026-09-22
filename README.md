# linkling-api

The Linkling service: the short-link API, redirects, click counts, the stats page and the CLI.

Part of **Linkling**, a small self-hosted link shortener built by the Tinyworks program.
Every decision with reach is written down first, in `docs/adr/`, and the code is built to it.

**Durability is the deployer's.** The link database is a single SQLite file at the path you
give in `LINKLING_DB`, and nothing here backs it up for you — see `docs/adr/0007-compose-topology.md`.

## What exists today

Creating a link (with a name you choose or one the service invents), following it, and
deleting it. Counting, expiry, the stats page and the CLI are separate pieces of work and
are not here yet.

| | |
|---|---|
| `POST /-/api/links` | `{"url": …, "name": …?, "created_by": …?}` → `201 {"name", "url"}`. Needs the team key. |
| `DELETE /-/api/links/<name>` | `204`. Needs the team key. A deleted name stays reserved forever. |
| `GET\|HEAD /<name>` | `302` to the long URL with `Cache-Control: no-store`. No credential, no cookie. |

Unknown names answer `404`, deleted ones `410`, and every response the application
produces carries `Cache-Control: no-store` — a 500 from the framework's own error handler
is the one exception, and 500 is not a cacheable status. The decisions are in
`docs/adr/0001`, `0003`, `0005` and `0006`.

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
