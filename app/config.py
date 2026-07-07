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

    # Root logging level (DEBUG, INFO, WARNING, ...).
    log_level: str = "INFO"

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
