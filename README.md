# stac-federated

A federated **STAC catalog discovery service**. Earth-observation data is
scattered across many independent STAC catalogs — NASA CMR-STAC, Microsoft
Planetary Computer, AWS Earth Search — each queried separately, with no unified
interface or relevance ranking. `stac-federated` fans a single search out to all
of them concurrently, normalizes the heterogeneous responses into one schema,
ranks results by semantic relevance, and streams them back.

This repository is under active development. **This is Phase 1 — the
foundation.**

## What Phase 1 sets up

Phase 1 is the skeleton the rest of the system is built on. It does **not** yet
perform real searches. It provides:

- **Project structure & tooling** — FastAPI app, Docker Compose (FastAPI +
  Postgres with the `pgvector` extension), `pyproject.toml`, Alembic migrations.
- **Core schemas** (Pydantic v2) — `STACSearchRequest`, `STACSearchResponse`,
  and the unified `NormalizedSTACItem` that every catalog response will map into.
- **Adapter interface** — the abstract `STACAdapter` base class every catalog
  adapter will implement in Phase 2.
- **Provider registry** — a Postgres `providers` table plus the migration that
  installs `pgvector` and creates the table.
- **Provider discovery** — on startup the service fetches the NASA CMR-STAC root,
  registers every child provider, and seeds the static providers (Planetary
  Computer, Earth Search). Discovery is idempotent (upsert on `base_url`).
- **Endpoints**
  - `GET /health` — liveness probe.
  - `GET /catalogs` — the registered, active providers.
  - `POST /search` — accepts a full `STACSearchRequest` and returns an empty
    (but well-formed) `STACSearchResponse`. Real fan-out lands in Phase 2+.

### Explicitly out of scope for Phase 1

Search fan-out, catalog adapters, response normalization, RemoteCLIP embeddings,
pgvector similarity queries, SSE streaming, and deployment config all come in
later phases.

## Running locally

Prerequisites: Docker + Docker Compose.

```bash
docker compose up --build
```

This starts two services:

- `postgres` — `pgvector/pgvector:pg15` (extension pre-installed), with a
  healthcheck.
- `app` — the FastAPI service, which waits for Postgres to be healthy, runs
  `alembic upgrade head`, then serves on port **8000**.

Once up:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/catalogs
curl -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"bbox": [-122.5, 37.7, -122.3, 37.9], "datetime": "2024-01-01/2024-12-31", "text": "wildfire"}'
```

Interactive API docs are at <http://localhost:8000/docs>.

## Running without Docker

You need a Postgres 15 instance with the `pgvector` extension available.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # then edit DATABASE_URL if needed
alembic upgrade head
uvicorn app.main:app --reload
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable                    | Default                                   | Description                                  |
| --------------------------- | ----------------------------------------- | -------------------------------------------- |
| `DATABASE_URL`              | `postgresql://postgres:postgres@localhost:5432/stac` | Postgres connection string.       |
| `CMR_STAC_ROOT`             | `https://cmr.earthdata.nasa.gov/stac/`    | CMR-STAC root used for provider discovery.   |
| `DISCOVERY_TIMEOUT_SECONDS` | `30`                                      | Timeout for discovery HTTP calls.            |
| `LOG_LEVEL`                 | `INFO`                                    | Root log level.                              |

## Project layout

```
app/
  main.py            # FastAPI app init, lifespan, router inclusion
  config.py          # Settings via pydantic-settings
  database.py        # asyncpg pool setup
  models/provider.py # Provider registry data access (raw asyncpg)
  schemas/           # Pydantic request/response/item schemas
  adapters/base.py   # Abstract STACAdapter base class
  services/discovery.py  # Provider discovery (CMR + static seeds)
  routes/            # health, catalogs, search
alembic/             # Migrations (pgvector extension + providers table)
docker-compose.yml   # app + postgres (pgvector)
Dockerfile
```
