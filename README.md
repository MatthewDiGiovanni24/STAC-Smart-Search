# STAC Smart Search

A federated **STAC catalog discovery service** with a modern web frontend. Earth-observation data is scattered across many independent STAC catalogs — NASA CMR-STAC, Microsoft Planetary Computer, AWS Earth Search — each queried separately, with no unified interface or relevance ranking. 

`STAC Smart Search` solves this using a **Two-Stage Hybrid Search**:
1. **Semantic & Spatial Pre-filtering**: It uses a local `pgvector` database and the RemoteCLIP AI model to find the most semantically relevant datasets that actually overlap your requested Bounding Box and Date.


Vector Map

<img src="assets/vectors.png" alt="Vectors Plot" width="400">


2. **Progressive Fanout**: It streams requests to the underlying APIs in chunks, dynamically falling back to lower-ranked datasets if top matches have no imagery, and streams normalized results back to the frontend in real-time.

## Running the Backend (Docker)

```bash
# Tear down any old database volumes (if schemas changed) and rebuild:
docker compose down -v --rmi local
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

## Known behavior

- **Planetary Computer requires `collections` on `/search`.** Unlike CMR and
  Earth Search, PC returns `422 "collection is required"` for a collection-less
  catalog-wide search. This is resolved architecturally: the collection
  pre-filter always scopes each provider request to specific collection ids, so
  PC receives `collections` in normal operation. (A bare, unscoped call to PC
  will still 422 — that's PC's API policy, not a bug in our request encoding.)
- **Planetary Computer `/collections` can return `504` during upstream
  outages.** The collection crawler retries transient timeouts/5xx and degrades
  gracefully (PC is skipped that run; other catalogs proceed). If PC's
  collections aren't in the registry, PC simply isn't shortlisted until the next
  successful crawl.

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
