"""Integration test fixtures: real Postgres via testcontainers.

Requires Docker to be running.
Run with: pytest tests/integration -v
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sleep.domain.orm import Base


@pytest.fixture(scope="session")
def pg_container():
    """Session-scoped PostgreSQL container."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(pg_container):
    """Async connection URL for the testcontainers Postgres instance."""
    # testcontainers gives us a psycopg2 URL; convert to asyncpg
    url = pg_container.get_connection_url()
    return url.replace("psycopg2", "asyncpg")


@pytest.fixture
async def async_engine(pg_url):
    """Create engine and initialize schema."""
    engine = create_async_engine(pg_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(async_engine) -> AsyncSession:
    """Provide a transactional async session that rolls back after each test."""
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
