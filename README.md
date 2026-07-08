# stac-federated

A federated **STAC catalog discovery service**. Earth-observation data is
scattered across many independent STAC catalogs — NASA CMR-STAC, Microsoft
Planetary Computer, AWS Earth Search — each queried separately, with no unified
interface or relevance ranking. `stac-federated` fans a single search out to all
of them concurrently, normalizes the heterogeneous responses into one schema,
ranks results by semantic relevance, and streams them back.

This repository is under active development.

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
