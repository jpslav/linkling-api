# The Linkling service's image. How it is assembled is ADR-0015; the topology it sits in is
# ADR-0007. There is deliberately no VOLUME instruction: that would give /data an anonymous
# volume whenever nothing is mounted there, which is one `docker compose down -v` from gone.
FROM python:3.12-slim

# sqlite3 is here for the backup command the README documents (ADR-0007b). It runs inside this
# container, on the same host as the service, because the service opens the database in WAL
# mode and WAL requires that (https://sqlite.org/wal.html).
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 linkling \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin linkling

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY --chmod=755 deploy/entrypoint.sh /usr/local/bin/linkling-entrypoint

# stdout is a pipe here, so Python would block-buffer it, and uvicorn re-raises the SIGTERM it
# caught, so a buffer is never flushed on stop. Anything written there would then never reach
# `docker compose logs`, and a check that those logs hold no client address would pass
# without having seen it (tests/test_ll004_no_trace.py measured the same thing).
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["linkling-entrypoint"]
# The README's own invocation, with only the host changed. --no-access-log is ADR-0012's
# requirement: uvicorn's access log writes every clicker's IP address.
CMD ["uvicorn", "--factory", "linkling.server.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
