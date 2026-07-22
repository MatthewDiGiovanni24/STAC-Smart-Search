"""Application configuration loaded from environment variables.

Uses pydantic-settings so every setting is validated and typed. Values are
read from the process environment (and a local ``.env`` file if present).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the stac-federated service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres connection string in libpq/asyncpg form, e.g.
    # ``postgresql://user:pass@host:5432/dbname``. Alembic converts this to the
    # ``postgresql+asyncpg://`` form internally when it needs a SQLAlchemy URL.
    database_url: str = "postgresql://postgres:postgres@localhost:5432/stac"

    # Root of the NASA CMR-STAC catalog. Provider discovery walks its ``links``.
    cmr_stac_root: str = "https://cmr.earthdata.nasa.gov/stac/"

    # How long (seconds) provider discovery HTTP calls may take before timing out.
    discovery_timeout_seconds: int = 30

    # Per-catalog search timeout (seconds) used by adapters during fan-out.
    adapter_timeout_seconds: int = 10

    # Root logging level (DEBUG, INFO, WARNING, ...).
    log_level: str = "INFO"

    # --- RemoteCLIP embedding settings ------------------------------------
    # open_clip architecture and the RemoteCLIP checkpoint fetched from the HF
    # hub. ViT-B-32 yields 512-dim text embeddings (matches the pgvector column).
    embedding_model_name: str = "ViT-B-32"
    remoteclip_repo: str = "chendelong/RemoteCLIP"
    remoteclip_checkpoint: str = "RemoteCLIP-ViT-B-32.pt"
    # Torch device: "cpu", "cuda", or "mps". "auto" picks the best available.
    embedding_device: str = "auto"
    # Batch size for encoding many collection descriptions at once.
    embedding_batch_size: int = 64

    # --- Collection registry / startup crawl ------------------------------
    # Whether the startup lifespan kicks off a background registry crawl.
    registry_refresh_on_startup: bool = True
    # Skip the startup crawl if the registry was refreshed within this window.
    registry_ttl_hours: int = 24
    # Dev cap on how many CMR collections to crawl (None = all ~55k).
    max_cmr_collections: int | None = None
    # Number of shortlisted collections the pre-filter returns per query.
    candidate_limit: int = 10000

    # Kill-switch for item-level semantic reranking (Phase 5).
    ranking_enabled: bool = True

    # Cosine distance threshold for semantic reranking. Items with a distance
    # above this threshold are filtered out. Lower = more strict, higher = more permissive. 0.0 = only exact matches, 1.0 = all items pass
    cosine_distance_threshold: float = 0.25

    @property
    def sqlalchemy_url(self) -> str:
        """Return the DATABASE_URL in the ``postgresql+asyncpg://`` form Alembic expects."""
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def asyncpg_url(self) -> str:
        """Return the DATABASE_URL in the plain libpq form ``asyncpg.create_pool`` expects."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (parsed once per process)."""
    return Settings()
