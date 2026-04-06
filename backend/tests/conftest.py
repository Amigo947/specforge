"""
Shared pytest fixtures for SpecForge backend tests.

Architecture decisions:
- Each test gets a brand-new SQLite in-memory engine so commits inside CRUD
  functions (e.g. crud.user.create_user) never bleed between tests.
- The FastAPI app's get_db dependency is overridden in every router test so
  the MySQL engine defined in app.db.database is never touched during tests.
- httpx.AsyncClient + ASGITransport does NOT trigger ASGI lifespan events,
  so the MySQL create_all in main.py's lifespan never runs.
- AI service calls are patched at the httpx transport layer, not at import
  time, giving explicit control over each test's network behaviour.
"""
from __future__ import annotations

import os
import pytest
import pytest_asyncio
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

# ── env vars must be set before any app import ────────────────────────────────
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-32-chars-long!!")
os.environ.setdefault("AI_SERVICE_URL", "http://mock-ai-service")

from httpx import AsyncClient, ASGITransport                        # noqa: E402
from sqlalchemy.ext.asyncio import (                                # noqa: E402
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)

from app.main import app                                            # noqa: E402
from app.db.session import get_db                                   # noqa: E402
from app.models import Base                                         # noqa: E402


# ── per-test in-memory SQLite engine ─────────────────────────────────────────

@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    """
    Fresh SQLite :memory: engine per test.

    Using aiosqlite means no network, no MySQL, no shared state.
    Tables are created from the live ORM metadata so schema drift is caught
    automatically.  The engine is disposed after the test to release memory.
    """
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield test_engine

    await test_engine.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """
    Async SQLAlchemy session backed by the per-test SQLite engine.

    expire_on_commit=False prevents lazy-load errors after flush/commit inside
    CRUD functions that call db.refresh() on the returned object.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── FastAPI test client ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """
    httpx.AsyncClient wired to the FastAPI app.

    get_db is overridden so every route handler operates on the same
    per-test SQLite session.  Dependency overrides are cleared after each
    test so they never leak across fixtures.
    """
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client

    app.dependency_overrides.clear()


# ── realistic domain data ─────────────────────────────────────────────────────

@pytest.fixture
def sample_project_payload() -> dict:
    """Minimal valid ProjectCreate payload for a FastAPI spec."""
    return {
        "name": "SpecForge Demo API",
        "description": "Manages API documentation generation",
        "source_type": "RAW_CODE",
        "source_code": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            "@app.get('/users')\n"
            "def list_users(): ...\n\n"
            "@app.post('/users')\n"
            "def create_user(): ...\n"
        ),
        "framework": "fastapi",
    }


@pytest.fixture
def ai_parse_response() -> dict:
    """
    Realistic response body returned by the AI service POST /parse endpoint.
    Contains two endpoints with nested parameters.
    """
    return {
        "endpoints": [
            {
                "path": "/users",
                "method": "GET",
                "summary": "List all users",
                "description": "Returns a paginated list of registered users.",
                "tag": "Users",
                "parameters": [
                    {
                        "name": "page",
                        "location": "QUERY",
                        "type": "integer",
                        "required": False,
                        "description": "Page number (1-indexed)",
                        "schema": {"type": "integer", "minimum": 1},
                    },
                    {
                        "name": "limit",
                        "location": "QUERY",
                        "type": "integer",
                        "required": False,
                        "description": "Max results per page",
                    },
                ],
            },
            {
                "path": "/users/{id}",
                "method": "GET",
                "summary": "Get user by ID",
                "description": None,
                "tag": "Users",
                "parameters": [
                    {
                        "name": "id",
                        "location": "PATH",
                        "type": "string",
                        "required": True,
                        "description": "User CUID",
                    }
                ],
            },
        ]
    }


@pytest.fixture
def ai_generate_response() -> dict:
    """
    Realistic response body returned by the AI service POST /generate endpoint.
    Contains request + response examples and a markdown documentation page.
    """
    return {
        "examples": [
            {
                "type": "REQUEST",
                "title": "Fetch first page",
                "language": "http",
                "code": "GET /users?page=1&limit=20 HTTP/1.1\nHost: api.example.com",
                "status_code": None,
            },
            {
                "type": "RESPONSE",
                "title": "200 OK — success",
                "language": "json",
                "code": '{"success": true, "data": [{"id": "cuid1", "name": "Alice"}]}',
                "status_code": 200,
            },
            {
                "type": "ERROR",
                "title": "401 Unauthorized",
                "language": "json",
                "code": '{"success": false, "error": {"code": "UNAUTHORIZED"}}',
                "status_code": 401,
            },
        ],
        "documentation": {
            "title": "GET /users — List Users",
            "content": (
                "## List Users\n\n"
                "Returns a paginated list of users.\n\n"
                "### Query Parameters\n"
                "| Name  | Type    | Required | Description    |\n"
                "|-------|---------|----------|----------------|\n"
                "| page  | integer | No       | Page number    |\n"
                "| limit | integer | No       | Results / page |\n"
            ),
        },
    }


# ── mock httpx client for AI service tests ───────────────────────────────────

@pytest.fixture
def mock_httpx_response() -> MagicMock:
    """
    Pre-built mock for a successful httpx.Response.
    Callers can override .json.return_value to inject custom payloads.
    """
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"endpoints": []}
    return resp


@pytest.fixture
def mock_ai_http_client(mock_httpx_response: MagicMock) -> MagicMock:
    """
    Mock httpx.AsyncClient whose .request() returns a successful response.
    Inject this via monkeypatching app.services.ai_client._get_client.
    """
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=mock_httpx_response)
    return mock_client
