"""
tests/test_crud.py — Async CRUD layer tests against in-memory SQLite.

Each test function receives a fresh, isolated in-memory database via the
`db` fixture defined in conftest.py.  No test can affect another.

Coverage:
- project CRUD: create, get by id, list with pagination, update name/status,
                delete, non-existent id returns None
- endpoint CRUD: batch create with parameters, get by id, get by project,
                 add examples, search
- user CRUD:     create, get by email, get by id, password hashing
- doc_page CRUD: create, list by project, get by slug, update
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import project as project_crud
from app.crud import endpoint as endpoint_crud
from app.crud import user as user_crud
from app.crud import doc_page as doc_page_crud
from app.models.enums import ProjectStatus, HttpMethod
from app.schemas.auth import UserCreate
from app.schemas.endpoint import EndpointCreate
from app.schemas.example import ExampleCreate
from app.schemas.parameter import ParameterCreate


# ── shared helpers ────────────────────────────────────────────────────────────

async def _create_project(db: AsyncSession, name: str = "Test API") -> object:
    """Insert one project and return the ORM object."""
    return await project_crud.create_project(
        db,
        name=name,
        description="A test project",
        source_type="RAW_CODE",
        source_code="from fastapi import FastAPI\napp = FastAPI()",
        framework="fastapi",
    )


async def _create_endpoint(
    db: AsyncSession,
    project_id: str,
    path: str = "/items",
    method: str = "GET",
) -> object:
    """Insert one endpoint under project_id and return the ORM object."""
    results = await endpoint_crud.create_endpoints_batch(
        db,
        project_id,
        [EndpointCreate(path=path, method=method, summary="Test", tag="Items")],
    )
    return results[0]


# ══════════════════════════════════════════════════════════════════════════════
# Project CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestProjectCrud:
    @pytest.mark.asyncio
    async def test_create_project_returns_orm_object_with_generated_id(
        self, db: AsyncSession
    ):
        """create_project inserts a row and returns an ORM object with a CUID id."""
        project = await _create_project(db)
        assert project.id is not None
        assert project.id.startswith("c")  # CUID prefix
        assert project.name == "Test API"
        assert project.status == ProjectStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_project_by_id_returns_existing_project(
        self, db: AsyncSession
    ):
        """get_project_by_id returns the correct project for a known id."""
        created = await _create_project(db, name="FindMe")
        fetched = await project_crud.get_project_by_id(db, created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "FindMe"

    @pytest.mark.asyncio
    async def test_get_project_by_id_returns_none_for_unknown_id(
        self, db: AsyncSession
    ):
        """get_project_by_id returns None (not an exception) for an unknown id."""
        result = await project_crud.get_project_by_id(db, "nonexistent_id_00000")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_project_by_id_eagerly_loads_endpoints_and_docs(
        self, db: AsyncSession
    ):
        """get_project_by_id eagerly loads endpoints and documentation_pages."""
        created = await _create_project(db)
        await _create_endpoint(db, created.id)

        fetched = await project_crud.get_project_by_id(db, created.id)
        assert fetched is not None
        # selectinload must have populated the relationship
        assert isinstance(fetched.endpoints, list)
        assert len(fetched.endpoints) == 1

    @pytest.mark.asyncio
    async def test_list_projects_returns_all_projects_with_total(
        self, db: AsyncSession
    ):
        """list_projects returns (list[Project], total_count) for all rows."""
        await _create_project(db, "Alpha")
        await _create_project(db, "Beta")
        await _create_project(db, "Gamma")

        projects, total = await project_crud.list_projects(db, page=1, limit=10)
        assert total == 3
        assert len(projects) == 3

    @pytest.mark.asyncio
    async def test_list_projects_pagination_offset_respected(
        self, db: AsyncSession
    ):
        """Pagination: page=2 with limit=2 skips the first 2 rows."""
        for i in range(5):
            await _create_project(db, f"Project {i}")

        page2, total = await project_crud.list_projects(db, page=2, limit=2)
        assert total == 5
        assert len(page2) == 2

    @pytest.mark.asyncio
    async def test_list_projects_filtered_by_status(self, db: AsyncSession):
        """list_projects returns only projects whose status matches the filter."""
        proj = await _create_project(db, "Parsed")
        await project_crud.update_project_status(db, proj.id, ProjectStatus.PARSED)
        await _create_project(db, "Pending")  # stays PENDING

        parsed_list, total = await project_crud.list_projects(
            db, page=1, limit=10, status="PARSED"
        )
        assert total == 1
        assert parsed_list[0].name == "Parsed"

    @pytest.mark.asyncio
    async def test_update_project_name_persists_new_name(self, db: AsyncSession):
        """update_project_name changes the name field on the row."""
        project = await _create_project(db, "Old Name")
        updated = await project_crud.update_project_name(db, project.id, "New Name")
        assert updated is not None
        assert updated.name == "New Name"

    @pytest.mark.asyncio
    async def test_update_project_name_returns_none_for_unknown_id(
        self, db: AsyncSession
    ):
        """update_project_name returns None without error for a missing id."""
        result = await project_crud.update_project_name(
            db, "bad_id_99999", "Ghost"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_project_status_transitions_correctly(
        self, db: AsyncSession
    ):
        """update_project_status changes status from PENDING to PARSED."""
        project = await _create_project(db)
        assert project.status == ProjectStatus.PENDING

        updated = await project_crud.update_project_status(
            db, project.id, ProjectStatus.PARSED
        )
        assert updated is not None
        assert updated.status == ProjectStatus.PARSED

    @pytest.mark.asyncio
    async def test_delete_project_returns_true_and_row_is_gone(
        self, db: AsyncSession
    ):
        """delete_project removes the row; subsequent get returns None."""
        project = await _create_project(db)
        deleted = await project_crud.delete_project(db, project.id)
        assert deleted is True

        fetched = await project_crud.get_project_by_id(db, project.id)
        assert fetched is None

    @pytest.mark.asyncio
    async def test_delete_project_returns_false_for_unknown_id(
        self, db: AsyncSession
    ):
        """delete_project returns False (not an error) for an unknown id."""
        result = await project_crud.delete_project(db, "no_such_project_9999")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestEndpointCrud:
    @pytest.mark.asyncio
    async def test_create_endpoints_batch_inserts_all_rows(
        self, db: AsyncSession
    ):
        """create_endpoints_batch creates multiple endpoints in one call."""
        project = await _create_project(db)
        ep_schemas = [
            EndpointCreate(path="/users", method="GET", summary="List users", tag="Users"),
            EndpointCreate(path="/users", method="POST", summary="Create user", tag="Users"),
            EndpointCreate(path="/users/{id}", method="DELETE", summary="Delete user", tag="Users"),
        ]
        created = await endpoint_crud.create_endpoints_batch(
            db, project.id, ep_schemas
        )
        assert len(created) == 3
        paths = {ep.path for ep in created}
        assert "/users" in paths
        assert "/users/{id}" in paths

    @pytest.mark.asyncio
    async def test_create_endpoint_with_nested_parameters(
        self, db: AsyncSession
    ):
        """Nested ParameterCreate schemas are persisted alongside the endpoint."""
        project = await _create_project(db)
        ep_schema = EndpointCreate(
            path="/search",
            method="GET",
            summary="Search",
            tag="Search",
            parameters=[
                ParameterCreate(
                    name="q",
                    location="QUERY",
                    type="string",
                    required=True,
                    description="Search query",
                    schema_def={"type": "string"},
                ),
                ParameterCreate(
                    name="page",
                    location="QUERY",
                    type="integer",
                    required=False,
                ),
            ],
        )
        created = await endpoint_crud.create_endpoints_batch(db, project.id, [ep_schema])
        endpoint = created[0]

        # Re-fetch to ensure selectinload works
        fetched = await endpoint_crud.get_endpoint_by_id(db, endpoint.id)
        assert fetched is not None
        assert len(fetched.parameters) == 2
        names = {p.name for p in fetched.parameters}
        assert names == {"q", "page"}

    @pytest.mark.asyncio
    async def test_get_endpoint_by_id_returns_endpoint(self, db: AsyncSession):
        """get_endpoint_by_id fetches an endpoint with its nested relations."""
        project = await _create_project(db)
        endpoint = await _create_endpoint(db, project.id)
        fetched = await endpoint_crud.get_endpoint_by_id(db, endpoint.id)
        assert fetched is not None
        assert fetched.id == endpoint.id
        assert fetched.path == "/items"

    @pytest.mark.asyncio
    async def test_get_endpoint_by_id_returns_none_for_unknown_id(
        self, db: AsyncSession
    ):
        """get_endpoint_by_id returns None for a non-existent endpoint id."""
        result = await endpoint_crud.get_endpoint_by_id(db, "ghost_endpoint_000")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_endpoints_by_project_returns_correct_rows(
        self, db: AsyncSession
    ):
        """get_endpoints_by_project returns only endpoints for the given project."""
        proj_a = await _create_project(db, "Project A")
        proj_b = await _create_project(db, "Project B")

        await _create_endpoint(db, proj_a.id, "/a1")
        await _create_endpoint(db, proj_a.id, "/a2")
        await _create_endpoint(db, proj_b.id, "/b1")

        endpoints_a = await endpoint_crud.get_endpoints_by_project(db, proj_a.id)
        assert len(endpoints_a) == 2
        assert all(ep.project_id == proj_a.id for ep in endpoints_a)

    @pytest.mark.asyncio
    async def test_add_examples_to_endpoint_persists_examples(
        self, db: AsyncSession
    ):
        """add_examples_to_endpoint appends Example rows to an endpoint."""
        project = await _create_project(db)
        endpoint = await _create_endpoint(db, project.id)

        example_schemas = [
            ExampleCreate(
                type="RESPONSE",
                title="200 OK",
                language="json",
                code='{"success": true}',
                status_code=200,
            ),
            ExampleCreate(
                type="ERROR",
                title="404 Not Found",
                language="json",
                code='{"success": false}',
                status_code=404,
            ),
        ]
        examples = await endpoint_crud.add_examples_to_endpoint(
            db, endpoint.id, example_schemas
        )
        assert len(examples) == 2
        types = {ex.type for ex in examples}
        assert "RESPONSE" in types
        assert "ERROR" in types

    @pytest.mark.asyncio
    async def test_search_endpoints_by_path_fragment(self, db: AsyncSession):
        """search_endpoints matches endpoints whose path contains the query."""
        project = await _create_project(db)
        await _create_endpoint(db, project.id, "/api/users")
        await _create_endpoint(db, project.id, "/api/projects", "POST")

        results, total = await endpoint_crud.search_endpoints(db, "users")
        assert total == 1
        assert results[0].path == "/api/users"

    @pytest.mark.asyncio
    async def test_search_endpoints_scoped_to_project(self, db: AsyncSession):
        """search_endpoints with project_id filters to that project only."""
        proj_a = await _create_project(db, "A")
        proj_b = await _create_project(db, "B")
        await _create_endpoint(db, proj_a.id, "/widgets")
        await _create_endpoint(db, proj_b.id, "/widgets", "POST")

        results, total = await endpoint_crud.search_endpoints(
            db, "widgets", project_id=proj_a.id
        )
        assert total == 1
        assert results[0].project_id == proj_a.id

    @pytest.mark.asyncio
    async def test_search_endpoints_no_match_returns_empty_list(
        self, db: AsyncSession
    ):
        """search_endpoints returns ([], 0) when nothing matches the query."""
        project = await _create_project(db)
        await _create_endpoint(db, project.id, "/users")

        results, total = await endpoint_crud.search_endpoints(db, "zzz_no_match")
        assert total == 0
        assert results == []


# ══════════════════════════════════════════════════════════════════════════════
# User CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestUserCrud:
    @pytest.mark.asyncio
    async def test_create_user_hashes_password_and_returns_user(
        self, db: AsyncSession
    ):
        """
        create_user stores a bcrypt hash (not plaintext) and returns a User
        ORM object with a generated CUID id.
        """
        data = UserCreate(
            name="Alice", email="alice@example.com", password="securepass1"
        )
        user = await user_crud.create_user(db, data)
        assert user.id is not None
        assert user.name == "Alice"
        assert user.email == "alice@example.com"
        # Password must NOT be stored in plaintext
        assert user.password_hash != "securepass1"
        # Hash must be verifiable
        assert user_crud.verify_password("securepass1", user.password_hash)

    @pytest.mark.asyncio
    async def test_get_user_by_email_returns_existing_user(
        self, db: AsyncSession
    ):
        """get_user_by_email returns the User object for a registered email."""
        data = UserCreate(
            name="Bob", email="bob@example.com", password="password99"
        )
        created = await user_crud.create_user(db, data)
        found = await user_crud.get_user_by_email(db, "bob@example.com")
        assert found is not None
        assert found.id == created.id

    @pytest.mark.asyncio
    async def test_get_user_by_email_returns_none_for_unknown_email(
        self, db: AsyncSession
    ):
        """get_user_by_email returns None (not an exception) for unknown email."""
        result = await user_crud.get_user_by_email(db, "ghost@nowhere.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_by_id_returns_existing_user(self, db: AsyncSession):
        """get_user_by_id fetches the User by primary key."""
        data = UserCreate(
            name="Carol", email="carol@example.com", password="carolpass"
        )
        created = await user_crud.create_user(db, data)
        found = await user_crud.get_user_by_id(db, created.id)
        assert found is not None
        assert found.email == "carol@example.com"

    @pytest.mark.asyncio
    async def test_get_user_by_id_returns_none_for_unknown_id(
        self, db: AsyncSession
    ):
        """get_user_by_id returns None for a non-existent user id."""
        result = await user_crud.get_user_by_id(db, "no_such_user_000000000")
        assert result is None

    def test_hash_password_produces_bcrypt_hash(self):
        """hash_password returns a bcrypt hash different from the plaintext."""
        hashed = user_crud.hash_password("mypassword")
        assert hashed != "mypassword"
        assert hashed.startswith("$2b$")

    def test_verify_password_correct_password_returns_true(self):
        """verify_password returns True when the plain password matches the hash."""
        hashed = user_crud.hash_password("correct_horse_battery")
        assert user_crud.verify_password("correct_horse_battery", hashed) is True

    def test_verify_password_wrong_password_returns_false(self):
        """verify_password returns False for an incorrect plaintext password."""
        hashed = user_crud.hash_password("correct_horse_battery")
        assert user_crud.verify_password("wrong_password", hashed) is False


# ══════════════════════════════════════════════════════════════════════════════
# Doc Page CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestDocPageCrud:
    @pytest.mark.asyncio
    async def test_create_doc_page_returns_page_with_id(self, db: AsyncSession):
        """create_doc_page inserts a row and returns a DocumentationPage ORM object."""
        project = await _create_project(db)
        page = await doc_page_crud.create_doc_page(
            db,
            project_id=project.id,
            title="Getting Started",
            slug="getting-started",
            content="## Getting Started\n\nWelcome to the API.",
            order=0,
        )
        assert page.id is not None
        assert page.title == "Getting Started"
        assert page.slug == "getting-started"
        assert page.order == 0

    @pytest.mark.asyncio
    async def test_get_doc_pages_by_project_returns_ordered_pages(
        self, db: AsyncSession
    ):
        """get_doc_pages_by_project returns pages ordered by the order field."""
        project = await _create_project(db)
        await doc_page_crud.create_doc_page(
            db,
            project_id=project.id,
            title="Auth",
            slug="auth",
            content="Auth docs",
            order=1,
        )
        await doc_page_crud.create_doc_page(
            db,
            project_id=project.id,
            title="Intro",
            slug="intro",
            content="Intro docs",
            order=0,
        )
        pages = await doc_page_crud.get_doc_pages_by_project(db, project.id)
        assert len(pages) == 2
        # Order by order asc → "intro" (0) comes before "auth" (1)
        assert pages[0].order == 0
        assert pages[1].order == 1

    @pytest.mark.asyncio
    async def test_get_doc_page_by_slug_returns_correct_page(
        self, db: AsyncSession
    ):
        """get_doc_page_by_slug returns the matching page for project + slug."""
        project = await _create_project(db)
        await doc_page_crud.create_doc_page(
            db,
            project_id=project.id,
            title="Errors",
            slug="error-reference",
            content="Error codes",
            order=2,
        )
        found = await doc_page_crud.get_doc_page_by_slug(
            db, project.id, "error-reference"
        )
        assert found is not None
        assert found.title == "Errors"

    @pytest.mark.asyncio
    async def test_get_doc_page_by_slug_returns_none_for_unknown_slug(
        self, db: AsyncSession
    ):
        """get_doc_page_by_slug returns None (not an exception) for missing slug."""
        project = await _create_project(db)
        result = await doc_page_crud.get_doc_page_by_slug(
            db, project.id, "does-not-exist"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_doc_page_persists_new_content(self, db: AsyncSession):
        """update_doc_page overwrites the specified keyword-argument fields."""
        project = await _create_project(db)
        page = await doc_page_crud.create_doc_page(
            db,
            project_id=project.id,
            title="Old Title",
            slug="old-title",
            content="Old content",
            order=0,
        )
        updated = await doc_page_crud.update_doc_page(
            db, page.id, title="New Title", content="New content"
        )
        assert updated is not None
        assert updated.title == "New Title"
        assert updated.content == "New content"

    @pytest.mark.asyncio
    async def test_update_doc_page_returns_none_for_unknown_id(
        self, db: AsyncSession
    ):
        """update_doc_page returns None (not an exception) for a missing page id."""
        result = await doc_page_crud.update_doc_page(
            db, "no_such_page_000000", title="Ghost"
        )
        assert result is None
