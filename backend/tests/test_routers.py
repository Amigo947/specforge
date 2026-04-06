"""
tests/test_routers.py — FastAPI router integration tests.

Uses httpx.AsyncClient via ASGITransport to send real HTTP requests to the
FastAPI app.  The `client` fixture (conftest.py) overrides get_db with an
in-memory SQLite session so no MySQL is needed.

ASGI lifespan events are NOT triggered by ASGITransport, so the MySQL
create_all in main.py's lifespan never runs — the SQLite engine in conftest
handles schema creation instead.

Coverage:
- GET  /api/health                    → 200 {status: "ok"}
- POST /api/auth/register             → 201 TokenResponse, sets cookie
- POST /api/auth/register (duplicate) → 400
- POST /api/auth/login                → 200 TokenResponse
- POST /api/auth/login (bad creds)   → 401
- POST /api/auth/logout               → 200, deletes cookie
- GET  /api/auth/me (valid cookie)   → 200 UserOut
- GET  /api/auth/me (no cookie)      → 401
- POST /api/projects                 → 201 SuccessResponse[ProjectOut]
- POST /api/projects (invalid body)  → 422 validation error
- GET  /api/projects                 → 200 PaginatedResponse
- GET  /api/projects?status=PENDING  → 200 filtered list
- GET  /api/projects/{id}            → 200 SuccessResponse[ProjectOut]
- GET  /api/projects/{id} (unknown)  → 404
- PATCH /api/projects/{id}           → 200 with updated name
- DELETE /api/projects/{id}          → 200 {"deleted": true}
- POST /api/projects/parse-api       → 200 with mocked AI
- POST /api/projects/generate-docs   → 200 with mocked AI
- GET  /api/endpoints/{id}           → 200 EndpointOut
- GET  /api/endpoints/{id} (unknown) → 404
- GET  /api/search?q=...             → 200 PaginatedResponse
- GET  /api/export/{id}/markdown     → 200 text/markdown
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.auth_service import create_access_token


# ── helpers ───────────────────────────────────────────────────────────────────

REGISTER_URL = "/api/auth/register"
LOGIN_URL    = "/api/auth/login"
LOGOUT_URL   = "/api/auth/logout"
ME_URL       = "/api/auth/me"
PROJECTS_URL = "/api/projects"


def _project_body(**overrides) -> dict:
    defaults = {
        "name": "SpecForge API",
        "description": "AI-powered doc generator",
        "source_type": "RAW_CODE",
        "source_code": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            "@app.get('/users')\n"
            "def list_users(): ...\n"
        ),
        "framework": "fastapi",
    }
    return {**defaults, **overrides}


# ══════════════════════════════════════════════════════════════════════════════
# Health check
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_200_with_status_ok(self, client: AsyncClient):
        """GET /api/health returns 200 with {status: 'ok', timestamp: ...}."""
        response = await client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "timestamp" in body


# ══════════════════════════════════════════════════════════════════════════════
# Auth routes
# ══════════════════════════════════════════════════════════════════════════════

class TestAuthRegister:
    @pytest.mark.asyncio
    async def test_register_creates_user_and_returns_201(
        self, client: AsyncClient
    ):
        """
        POST /api/auth/register with valid payload returns 201,
        a TokenResponse body, and sets an httpOnly access_token cookie.
        """
        payload = {
            "name": "Alice Test",
            "email": "alice@specforge.test",
            "password": "securepass1",
        }
        response = await client.post(REGISTER_URL, json=payload)
        assert response.status_code == 201
        body = response.json()
        assert body["user"]["email"] == "alice@specforge.test"
        assert body["user"]["name"] == "Alice Test"
        assert "access_token" in response.cookies

    @pytest.mark.asyncio
    async def test_register_duplicate_email_returns_400(
        self, client: AsyncClient
    ):
        """
        Registering the same email twice returns 400 (email already registered).
        """
        payload = {
            "name": "Bob Test",
            "email": "bob@specforge.test",
            "password": "securepass1",
        }
        await client.post(REGISTER_URL, json=payload)
        response = await client.post(REGISTER_URL, json=payload)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_register_invalid_email_returns_422(
        self, client: AsyncClient
    ):
        """Invalid email format triggers Pydantic validation → 422."""
        response = await client.post(
            REGISTER_URL,
            json={"name": "Charlie", "email": "not-valid", "password": "pass12345"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_short_password_returns_422(
        self, client: AsyncClient
    ):
        """Password shorter than 8 chars triggers custom validator → 422."""
        response = await client.post(
            REGISTER_URL,
            json={"name": "Dave", "email": "d@test.com", "password": "short"},
        )
        assert response.status_code == 422


class TestAuthLogin:
    @pytest.mark.asyncio
    async def test_login_with_valid_credentials_returns_200(
        self, client: AsyncClient
    ):
        """
        POST /api/auth/login with correct credentials returns 200 and a
        TokenResponse body; the access_token cookie is refreshed.
        """
        payload = {
            "name": "Eve Login",
            "email": "eve@specforge.test",
            "password": "evepassword1",
        }
        await client.post(REGISTER_URL, json=payload)

        response = await client.post(
            LOGIN_URL,
            json={"email": "eve@specforge.test", "password": "evepassword1"},
        )
        assert response.status_code == 200
        assert response.json()["user"]["email"] == "eve@specforge.test"
        assert "access_token" in response.cookies

    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_401(self, client: AsyncClient):
        """Incorrect password returns 401 Unauthorized."""
        await client.post(
            REGISTER_URL,
            json={"name": "Frank", "email": "frank@test.com", "password": "frankpass"},
        )
        response = await client.post(
            LOGIN_URL,
            json={"email": "frank@test.com", "password": "WRONG_PASSWORD"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_unknown_email_returns_401(self, client: AsyncClient):
        """Unknown email returns 401 (same response to prevent email enumeration)."""
        response = await client.post(
            LOGIN_URL,
            json={"email": "ghost@nowhere.com", "password": "anything123"},
        )
        assert response.status_code == 401


class TestAuthLogout:
    @pytest.mark.asyncio
    async def test_logout_returns_200_and_clears_cookie(
        self, client: AsyncClient
    ):
        """POST /api/auth/logout returns 200 and deletes the access_token cookie."""
        response = await client.post(LOGOUT_URL)
        assert response.status_code == 200
        assert response.json()["message"] == "Logged out successfully"


class TestAuthMe:
    @pytest.mark.asyncio
    async def test_get_me_with_valid_cookie_returns_user_out(
        self, client: AsyncClient, db: AsyncSession
    ):
        """
        GET /api/auth/me with a valid JWT cookie returns the UserOut schema
        for the authenticated user.
        """
        # Register to create the user in the test DB
        await client.post(
            REGISTER_URL,
            json={
                "name": "Grace Me",
                "email": "grace@specforge.test",
                "password": "gracepass1",
            },
        )
        # Fetch the cookie set by register
        token = client.cookies.get("access_token")
        assert token is not None

        response = await client.get(ME_URL, cookies={"access_token": token})
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "grace@specforge.test"

    @pytest.mark.asyncio
    async def test_get_me_without_cookie_returns_401(self, client: AsyncClient):
        """GET /api/auth/me with no cookie returns 401 Not Authenticated."""
        response = await client.get(ME_URL)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_me_with_invalid_token_returns_401(
        self, client: AsyncClient
    ):
        """GET /api/auth/me with a tampered token returns 401."""
        response = await client.get(
            ME_URL,
            cookies={"access_token": "invalid.token.value"},
        )
        assert response.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Project routes
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateProject:
    @pytest.mark.asyncio
    async def test_post_project_returns_201_with_success_response(
        self, client: AsyncClient
    ):
        """
        POST /api/projects with a valid ProjectCreate payload returns 201,
        success=True, and a ProjectOut with a CUID id.
        """
        response = await client.post(PROJECTS_URL, json=_project_body())
        assert response.status_code == 201
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["name"] == "SpecForge API"
        assert data["status"] == "PENDING"
        assert data["id"].startswith("c")
        assert "endpoints" in data
        assert "documentationPages" not in data  # snake_case — no alias configured

    @pytest.mark.asyncio
    async def test_post_project_missing_source_code_returns_422(
        self, client: AsyncClient
    ):
        """Omitting required source_code triggers Pydantic validation → 422."""
        response = await client.post(
            PROJECTS_URL,
            json={"name": "No Source"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_post_project_name_too_long_returns_422(
        self, client: AsyncClient
    ):
        """A name exceeding 100 chars triggers validation → 422."""
        response = await client.post(
            PROJECTS_URL,
            json=_project_body(name="x" * 101),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_post_project_source_code_too_short_returns_422(
        self, client: AsyncClient
    ):
        """source_code shorter than 10 chars triggers validation → 422."""
        response = await client.post(
            PROJECTS_URL,
            json=_project_body(source_code="tiny"),
        )
        assert response.status_code == 422


class TestListProjects:
    @pytest.mark.asyncio
    async def test_list_projects_returns_200_paginated_response(
        self, client: AsyncClient
    ):
        """
        GET /api/projects returns 200 with a PaginatedResponse containing
        a pagination meta object.
        """
        # Create a couple of projects first
        await client.post(PROJECTS_URL, json=_project_body(name="Alpha"))
        await client.post(PROJECTS_URL, json=_project_body(name="Beta"))

        response = await client.get(PROJECTS_URL)
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert isinstance(body["data"], list)
        assert len(body["data"]) >= 2
        assert "pagination" in body
        assert body["pagination"]["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_projects_pagination_limit_respected(
        self, client: AsyncClient
    ):
        """limit=1 returns at most 1 item per page."""
        await client.post(PROJECTS_URL, json=_project_body(name="P1"))
        await client.post(PROJECTS_URL, json=_project_body(name="P2"))

        response = await client.get(f"{PROJECTS_URL}?page=1&limit=1")
        assert response.status_code == 200
        assert len(response.json()["data"]) == 1

    @pytest.mark.asyncio
    async def test_list_projects_status_filter_applied(
        self, client: AsyncClient
    ):
        """
        GET /api/projects?status=PENDING returns only projects with PENDING status.
        All freshly created projects start as PENDING.
        """
        await client.post(PROJECTS_URL, json=_project_body(name="FilterTest"))
        response = await client.get(f"{PROJECTS_URL}?status=PENDING")
        assert response.status_code == 200
        body = response.json()
        assert all(p["status"] == "PENDING" for p in body["data"])

    @pytest.mark.asyncio
    async def test_list_projects_invalid_limit_returns_422(
        self, client: AsyncClient
    ):
        """limit > 50 violates the Query constraint → 422."""
        response = await client.get(f"{PROJECTS_URL}?limit=999")
        assert response.status_code == 422


class TestGetProject:
    @pytest.mark.asyncio
    async def test_get_project_by_id_returns_200_with_project_data(
        self, client: AsyncClient
    ):
        """
        GET /api/projects/{id} returns 200 with the full ProjectOut including
        empty endpoints and documentation_pages lists.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        response = await client.get(f"{PROJECTS_URL}/{project_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["id"] == project_id
        assert "endpoints" in body["data"]
        assert "documentation_pages" in body["data"]

    @pytest.mark.asyncio
    async def test_get_project_unknown_id_returns_404(
        self, client: AsyncClient
    ):
        """
        GET /api/projects/{id} for an unknown id returns 404 with
        error.code == 'NOT_FOUND'.
        """
        response = await client.get(f"{PROJECTS_URL}/nonexistent_cuid_000")
        assert response.status_code == 404
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "NOT_FOUND"


class TestRenameProject:
    @pytest.mark.asyncio
    async def test_patch_project_renames_and_returns_200(
        self, client: AsyncClient
    ):
        """
        PATCH /api/projects/{id} with ProjectRename body updates the name
        and returns the updated ProjectOut.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body(name="Old Name"))
        project_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"{PROJECTS_URL}/{project_id}",
            json={"name": "New Name"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["name"] == "New Name"

    @pytest.mark.asyncio
    async def test_patch_project_empty_name_returns_422(
        self, client: AsyncClient
    ):
        """An empty name string triggers Pydantic min_length validation → 422."""
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"{PROJECTS_URL}/{project_id}",
            json={"name": ""},
        )
        assert response.status_code == 422


class TestDeleteProject:
    @pytest.mark.asyncio
    async def test_delete_project_returns_200_with_deleted_true(
        self, client: AsyncClient
    ):
        """DELETE /api/projects/{id} returns 200 with data.deleted == true."""
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        response = await client.delete(f"{PROJECTS_URL}/{project_id}")
        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_project_then_get_returns_404(
        self, client: AsyncClient
    ):
        """After deletion, GET on the same id returns 404."""
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        await client.delete(f"{PROJECTS_URL}/{project_id}")
        get_resp = await client.get(f"{PROJECTS_URL}/{project_id}")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_unknown_project_returns_404(
        self, client: AsyncClient
    ):
        """DELETE for a non-existent project returns 404."""
        response = await client.delete(f"{PROJECTS_URL}/no_such_id_00000")
        assert response.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Parse-API route (mocked AI service)
# ══════════════════════════════════════════════════════════════════════════════

class TestParseApiRoute:
    @pytest.mark.asyncio
    async def test_post_parse_api_returns_200_with_endpoint_list(
        self, client: AsyncClient, ai_parse_response: dict
    ):
        """
        POST /api/projects/parse-api calls the AI service and returns
        SuccessResponse[list[EndpointOut]].
        The AI client is mocked to return a known parse response.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        with patch(
            "app.services.project_service.ai_client.parse_api",
            new=AsyncMock(return_value=ai_parse_response),
        ):
            response = await client.post(
                f"{PROJECTS_URL}/parse-api",
                json={"projectId": project_id},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert isinstance(body["data"], list)
        assert len(body["data"]) == 2  # ai_parse_response has 2 endpoints
        # Verify EndpointOut fields
        assert body["data"][0]["path"] == "/users"
        assert body["data"][0]["method"] == "GET"

    @pytest.mark.asyncio
    async def test_post_parse_api_accepts_camel_case_project_id(
        self, client: AsyncClient, ai_parse_response: dict
    ):
        """
        ProjectIdBody uses alias 'projectId'; the route must accept this
        camelCase field name from the JSON request body.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        with patch(
            "app.services.project_service.ai_client.parse_api",
            new=AsyncMock(return_value=ai_parse_response),
        ):
            response = await client.post(
                f"{PROJECTS_URL}/parse-api",
                json={"projectId": project_id},  # camelCase alias
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_post_parse_api_unknown_project_returns_404(
        self, client: AsyncClient
    ):
        """parse-api for an unknown projectId returns 404 NOT_FOUND."""
        response = await client.post(
            f"{PROJECTS_URL}/parse-api",
            json={"projectId": "no_such_project_000"},
        )
        assert response.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Generate-Docs route (mocked AI service)
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateDocsRoute:
    @pytest.mark.asyncio
    async def test_post_generate_docs_returns_200_with_project_out(
        self,
        client: AsyncClient,
        ai_parse_response: dict,
        ai_generate_response: dict,
    ):
        """
        POST /api/projects/generate-docs returns SuccessResponse[ProjectOut]
        after the full parse→generate cycle with mocked AI.
        """
        # 1. Create project
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        # 2. Parse API to populate endpoints (required before generate-docs)
        with patch(
            "app.services.project_service.ai_client.parse_api",
            new=AsyncMock(return_value=ai_parse_response),
        ):
            await client.post(
                f"{PROJECTS_URL}/parse-api",
                json={"projectId": project_id},
            )

        # 3. Generate docs
        with patch(
            "app.services.project_service.ai_client.generate_docs",
            new=AsyncMock(return_value=ai_generate_response),
        ):
            response = await client.post(
                f"{PROJECTS_URL}/generate-docs",
                json={"projectId": project_id},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["id"] == project_id
        assert body["data"]["status"] == "COMPLETED"

    @pytest.mark.asyncio
    async def test_post_generate_docs_wrong_status_returns_400(
        self, client: AsyncClient
    ):
        """
        generate-docs on a PENDING project (not yet parsed) returns 400
        with error.code == 'INVALID_STATUS'.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        response = await client.post(
            f"{PROJECTS_URL}/generate-docs",
            json={"projectId": project_id},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_STATUS"


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint routes
# ══════════════════════════════════════════════════════════════════════════════

class TestEndpointRoutes:
    @pytest.mark.asyncio
    async def test_get_endpoint_by_id_returns_200_endpoint_out(
        self, client: AsyncClient, ai_parse_response: dict
    ):
        """
        GET /api/endpoints/{id} returns 200 with an EndpointOut schema after
        the endpoint has been created via parse-api.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        with patch(
            "app.services.project_service.ai_client.parse_api",
            new=AsyncMock(return_value=ai_parse_response),
        ):
            parse_resp = await client.post(
                f"{PROJECTS_URL}/parse-api",
                json={"projectId": project_id},
            )

        endpoint_id = parse_resp.json()["data"][0]["id"]
        response = await client.get(f"/api/endpoints/{endpoint_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["id"] == endpoint_id
        assert "parameters" in body["data"]
        assert "examples" in body["data"]

    @pytest.mark.asyncio
    async def test_get_endpoint_unknown_id_returns_404(
        self, client: AsyncClient
    ):
        """GET /api/endpoints/{id} for an unknown id returns 404 NOT_FOUND."""
        response = await client.get("/api/endpoints/no_such_endpoint_000")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


# ══════════════════════════════════════════════════════════════════════════════
# Search route
# ══════════════════════════════════════════════════════════════════════════════

class TestSearchRoute:
    @pytest.mark.asyncio
    async def test_search_endpoints_returns_200_paginated_response(
        self, client: AsyncClient, ai_parse_response: dict
    ):
        """
        GET /api/search?q=users returns 200 with PaginatedResponse[EndpointOut]
        containing endpoints whose path matches the query.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        with patch(
            "app.services.project_service.ai_client.parse_api",
            new=AsyncMock(return_value=ai_parse_response),
        ):
            await client.post(
                f"{PROJECTS_URL}/parse-api",
                json={"projectId": project_id},
            )

        response = await client.get("/api/search?q=users")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert isinstance(body["data"], list)
        assert body["pagination"]["total"] >= 1

    @pytest.mark.asyncio
    async def test_search_missing_query_returns_422(self, client: AsyncClient):
        """GET /api/search without q param triggers validation → 422."""
        response = await client.get("/api/search")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_422(self, client: AsyncClient):
        """GET /api/search?q= (empty string) violates min_length=1 → 422."""
        response = await client.get("/api/search?q=")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_search_no_matches_returns_empty_list(
        self, client: AsyncClient
    ):
        """Searching for a string that matches nothing returns data=[] and total=0."""
        response = await client.get("/api/search?q=zzz_definitely_no_match")
        assert response.status_code == 200
        body = response.json()
        assert body["data"] == []
        assert body["pagination"]["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Export route
# ══════════════════════════════════════════════════════════════════════════════

class TestExportRoute:
    @pytest.mark.asyncio
    async def test_export_markdown_returns_text_markdown_content_type(
        self, client: AsyncClient
    ):
        """
        GET /api/export/{id}/markdown returns 200 with Content-Type text/markdown
        and a non-empty body for an existing project.
        """
        create_resp = await client.post(PROJECTS_URL, json=_project_body())
        project_id = create_resp.json()["data"]["id"]

        response = await client.get(f"/api/export/{project_id}/markdown")
        assert response.status_code == 200
        assert "text/markdown" in response.headers["content-type"]
        assert len(response.text) > 0
        # Project title should appear in the markdown
        assert "SpecForge API" in response.text

    @pytest.mark.asyncio
    async def test_export_markdown_unknown_project_returns_404(
        self, client: AsyncClient
    ):
        """export for a non-existent project id returns 404."""
        response = await client.get("/api/export/no_such_project_000/markdown")
        assert response.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Error response shape validation
# ══════════════════════════════════════════════════════════════════════════════

class TestErrorResponseShape:
    @pytest.mark.asyncio
    async def test_not_found_error_has_correct_json_structure(
        self, client: AsyncClient
    ):
        """
        All 404 errors from AppError handlers follow the
        {success: false, error: {code, message}} shape.
        """
        response = await client.get(f"{PROJECTS_URL}/nonexistent_000000000")
        assert response.status_code == 404
        body = response.json()
        assert body["success"] is False
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]
        assert body["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_422_validation_error_returns_fastapi_default_shape(
        self, client: AsyncClient
    ):
        """
        Pydantic ValidationError (422) uses FastAPI's default error format,
        not the AppError handler.  The response must have a 'detail' key.
        """
        response = await client.post(PROJECTS_URL, json={})
        assert response.status_code == 422
        body = response.json()
        assert "detail" in body
