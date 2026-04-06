"""
tests/test_services.py — Service layer tests with mocked CRUD + AI client.

All database and AI-service calls are replaced with AsyncMock / MagicMock so
tests run in-process with zero I/O.  Mocks are patched at the module level
of project_service so that every call the service makes goes through the mock.

Coverage:
- create_project: delegates to project_crud.create_project
- get_project: returns project on hit, raises NotFoundError on miss
- list_projects: returns (list, total) tuple from CRUD
- rename_project: verifies project exists before delegating rename
- delete_project: verifies project exists before delegating delete
- parse_api: full happy path (status PENDING→PARSING→PARSED)
- parse_api: AI service error → status set to FAILED, exception re-raised
- parse_api: AI returns invalid structure → FAILED + AppError raised
- generate_docs: full happy path (PARSED→GENERATING→COMPLETED)
- generate_docs: wrong status → raises AppError INVALID_STATUS
- generate_docs: AI error → FAILED + exception re-raised
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AIServiceError, AppError, NotFoundError
from app.models.enums import HttpMethod, ParamLocation, ProjectStatus
from app.services import project_service


# ── fixture helpers ───────────────────────────────────────────────────────────

def _mock_project(
    *,
    project_id: str = "cuid_proj_1",
    name: str = "Demo API",
    status: ProjectStatus = ProjectStatus.PENDING,
    source_code: str = "from fastapi import FastAPI\napp = FastAPI()",
    source_type: str = "RAW_CODE",
    framework: str | None = "fastapi",
    endpoints: list | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like a Project ORM object."""
    m = MagicMock()
    m.id = project_id
    m.name = name
    m.status = status
    m.source_code = source_code
    m.source_type = MagicMock(value=source_type)
    m.framework = framework
    m.endpoints = endpoints or []
    m.documentation_pages = []
    return m


def _mock_endpoint(
    *,
    endpoint_id: str = "cuid_ep_1",
    project_id: str = "cuid_proj_1",
    path: str = "/users",
    method_value: str = "GET",
    summary: str | None = "List users",
    description: str | None = None,
    parameters: list | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like an Endpoint ORM object."""
    m = MagicMock()
    m.id = endpoint_id
    m.project_id = project_id
    m.path = path
    m.method = MagicMock(value=method_value)
    m.summary = summary
    m.description = description
    m.parameters = parameters or []
    m.examples = []
    return m


# ── module-level patch targets ────────────────────────────────────────────────

_PROJ_CRUD = "app.services.project_service.project_crud"
_EP_CRUD   = "app.services.project_service.endpoint_crud"
_DOC_CRUD  = "app.services.project_service.doc_page_crud"
_AI        = "app.services.project_service.ai_client"


# ══════════════════════════════════════════════════════════════════════════════
# create_project
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateProject:
    @pytest.mark.asyncio
    async def test_create_project_delegates_to_crud(self):
        """create_project passes all kwargs directly to project_crud.create_project."""
        mock_project = _mock_project()
        db = AsyncMock()

        with patch(f"{_PROJ_CRUD}.create_project", new=AsyncMock(return_value=mock_project)) as mock_create:
            result = await project_service.create_project(
                db, user_id="cuid_user_1", name="Demo API", source_code="x" * 20
            )

        mock_create.assert_awaited_once_with(db, user_id="cuid_user_1", name="Demo API", source_code="x" * 20)
        assert result is mock_project


# ══════════════════════════════════════════════════════════════════════════════
# get_project
# ══════════════════════════════════════════════════════════════════════════════

class TestGetProject:
    @pytest.mark.asyncio
    async def test_get_project_returns_project_for_known_id(self):
        """get_project returns the project when CRUD finds it."""
        mock_project = _mock_project()
        db = AsyncMock()

        with patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=mock_project)):
            result = await project_service.get_project(db, "cuid_proj_1", "cuid_user_1")

        assert result is mock_project

    @pytest.mark.asyncio
    async def test_get_project_raises_not_found_for_unknown_id(self):
        """get_project raises NotFoundError when CRUD returns None."""
        db = AsyncMock()

        with patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=None)):
            with pytest.raises(NotFoundError) as exc_info:
                await project_service.get_project(db, "unknown_id", "cuid_user_1")

        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "NOT_FOUND"
        assert "Project" in exc_info.value.message


# ══════════════════════════════════════════════════════════════════════════════
# list_projects
# ══════════════════════════════════════════════════════════════════════════════

class TestListProjects:
    @pytest.mark.asyncio
    async def test_list_projects_returns_crud_result(self):
        """list_projects forwards pagination params to CRUD and returns its result."""
        projects = [_mock_project(project_id=f"cuid_{i}") for i in range(3)]
        db = AsyncMock()

        with patch(f"{_PROJ_CRUD}.list_projects", new=AsyncMock(return_value=(projects, 3))) as mock_list:
            result, total = await project_service.list_projects(db, "cuid_user_1", page=1, limit=10)

        mock_list.assert_awaited_once_with(db, user_id="cuid_user_1", page=1, limit=10, status=None)
        assert total == 3
        assert len(result) == 3


# ══════════════════════════════════════════════════════════════════════════════
# rename_project
# ══════════════════════════════════════════════════════════════════════════════

class TestRenameProject:
    @pytest.mark.asyncio
    async def test_rename_project_verifies_existence_then_updates_name(self):
        """rename_project first fetches the project, then calls update_project_name."""
        mock_project = _mock_project()
        renamed = _mock_project(name="New Name")
        db = AsyncMock()

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=mock_project)),
            patch(f"{_PROJ_CRUD}.update_project_name", new=AsyncMock(return_value=renamed)) as mock_rename,
        ):
            result = await project_service.rename_project(db, "cuid_proj_1", "New Name", "cuid_user_1")

        mock_rename.assert_awaited_once_with(db, "cuid_proj_1", "New Name", "cuid_user_1")
        assert result.name == "New Name"

    @pytest.mark.asyncio
    async def test_rename_project_raises_not_found_for_unknown_id(self):
        """rename_project raises NotFoundError before touching update_project_name."""
        db = AsyncMock()

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=None)),
            patch(f"{_PROJ_CRUD}.update_project_name", new=AsyncMock()) as mock_rename,
        ):
            with pytest.raises(NotFoundError):
                await project_service.rename_project(db, "bad_id", "Anything", "cuid_user_1")

        mock_rename.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# delete_project
# ══════════════════════════════════════════════════════════════════════════════

class TestDeleteProject:
    @pytest.mark.asyncio
    async def test_delete_project_verifies_existence_then_deletes(self):
        """delete_project checks the project exists before delegating to CRUD."""
        mock_project = _mock_project()
        db = AsyncMock()

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=mock_project)),
            patch(f"{_PROJ_CRUD}.delete_project", new=AsyncMock(return_value=True)) as mock_del,
        ):
            await project_service.delete_project(db, "cuid_proj_1", "cuid_user_1")

        mock_del.assert_awaited_once_with(db, "cuid_proj_1")

    @pytest.mark.asyncio
    async def test_delete_project_raises_not_found_for_unknown_id(self):
        """delete_project raises NotFoundError without touching delete_project CRUD."""
        db = AsyncMock()

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=None)),
            patch(f"{_PROJ_CRUD}.delete_project", new=AsyncMock()) as mock_del,
        ):
            with pytest.raises(NotFoundError):
                await project_service.delete_project(db, "bad_id", "cuid_user_1")

        mock_del.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# parse_api — happy path
# ══════════════════════════════════════════════════════════════════════════════

class TestParseApi:
    @pytest.mark.asyncio
    async def test_parse_api_happy_path_status_transitions_and_endpoint_creation(
        self, ai_parse_response: dict
    ):
        """
        parse_api: PENDING → PARSING, calls AI /parse, creates endpoints,
        transitions to PARSED, returns list of Endpoint ORM objects.
        """
        project = _mock_project()
        mock_endpoint = _mock_endpoint()
        db = AsyncMock()
        status_calls: list[ProjectStatus] = []

        async def _capture_status(db, pid, status):
            status_calls.append(status)
            return project

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(side_effect=_capture_status)),
            patch(f"{_AI}.parse_api", new=AsyncMock(return_value=ai_parse_response)) as mock_ai,
            patch(f"{_EP_CRUD}.create_endpoints_batch", new=AsyncMock(return_value=[mock_endpoint, mock_endpoint])) as mock_batch,
        ):
            result = await project_service.parse_api(db, project.id, "cuid_user_1")

        # Verify status transition order
        assert status_calls[0] == ProjectStatus.PARSING
        assert status_calls[1] == ProjectStatus.PARSED

        # AI was called with project's source code
        mock_ai.assert_awaited_once_with(
            source_code=project.source_code,
            source_type=project.source_type.value,
            framework=project.framework,
        )

        # Batch create was called with mapped EndpointCreate schemas
        mock_batch.assert_awaited_once()
        args = mock_batch.call_args
        endpoint_creates = args[0][2]  # third positional arg
        assert len(endpoint_creates) == 2  # matches ai_parse_response fixture

        assert result == [mock_endpoint, mock_endpoint]

    @pytest.mark.asyncio
    async def test_parse_api_maps_parameters_from_ai_response(
        self, ai_parse_response: dict
    ):
        """
        parse_api converts the AI response's parameter dicts into
        ParameterCreate schemas before passing them to create_endpoints_batch.
        """
        project = _mock_project()
        db = AsyncMock()

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(return_value=project)),
            patch(f"{_AI}.parse_api", new=AsyncMock(return_value=ai_parse_response)),
            patch(f"{_EP_CRUD}.create_endpoints_batch", new=AsyncMock(return_value=[])) as mock_batch,
        ):
            await project_service.parse_api(db, project.id, "cuid_user_1")

        endpoint_creates = mock_batch.call_args[0][2]
        # First endpoint has 2 parameters (from ai_parse_response fixture)
        assert len(endpoint_creates[0].parameters) == 2
        param_names = {p.name for p in endpoint_creates[0].parameters}
        assert "page" in param_names
        assert "limit" in param_names

    @pytest.mark.asyncio
    async def test_parse_api_ai_service_error_sets_failed_status(self):
        """
        When ai_client.parse_api raises AIServiceError, the project status
        is set to FAILED and the exception is re-raised to the caller.
        """
        project = _mock_project()
        db = AsyncMock()
        status_calls: list[ProjectStatus] = []

        async def _capture_status(db, pid, status):
            status_calls.append(status)
            return project

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(side_effect=_capture_status)),
            patch(f"{_AI}.parse_api", new=AsyncMock(side_effect=AIServiceError("AI down"))),
        ):
            with pytest.raises(AIServiceError, match="AI down"):
                await project_service.parse_api(db, project.id, "cuid_user_1")

        assert ProjectStatus.FAILED in status_calls

    @pytest.mark.asyncio
    async def test_parse_api_invalid_ai_response_sets_failed_status(self):
        """
        When AI returns a dict without an 'endpoints' list, the project
        status is set to FAILED and AppError(AI_INVALID_RESPONSE) is raised.
        """
        project = _mock_project()
        db = AsyncMock()
        status_calls: list[ProjectStatus] = []

        async def _capture_status(db, pid, status):
            status_calls.append(status)
            return project

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(side_effect=_capture_status)),
            patch(f"{_AI}.parse_api", new=AsyncMock(return_value={"not_endpoints": "bad"})),
        ):
            with pytest.raises(AppError) as exc_info:
                await project_service.parse_api(db, project.id, "cuid_user_1")

        assert exc_info.value.code == "AI_INVALID_RESPONSE"
        assert ProjectStatus.FAILED in status_calls


# ══════════════════════════════════════════════════════════════════════════════
# generate_docs
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateDocs:
    @pytest.mark.asyncio
    async def test_generate_docs_happy_path_creates_examples_and_doc_pages(
        self, ai_generate_response: dict
    ):
        """
        generate_docs: PARSED→GENERATING, calls AI /generate for each endpoint,
        creates Examples and DocumentationPage rows, transitions to COMPLETED,
        returns refreshed project.
        """
        endpoint = _mock_endpoint()
        project = _mock_project(status=ProjectStatus.PARSED, endpoints=[endpoint])
        db = AsyncMock()
        status_calls: list[ProjectStatus] = []

        async def _capture_status(db, pid, status):
            status_calls.append(status)
            return project

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(side_effect=_capture_status)),
            patch(f"{_EP_CRUD}.get_endpoints_by_project", new=AsyncMock(return_value=[endpoint])),
            patch(f"{_AI}.generate_docs", new=AsyncMock(return_value=ai_generate_response)) as mock_gen,
            patch(f"{_EP_CRUD}.add_examples_to_endpoint", new=AsyncMock(return_value=[])) as mock_ex,
            patch(f"{_DOC_CRUD}.create_doc_page", new=AsyncMock(return_value=MagicMock())) as mock_doc,
        ):
            result = await project_service.generate_docs(db, project.id, "cuid_user_1")

        # Status: GENERATING then COMPLETED
        assert status_calls[0] == ProjectStatus.GENERATING
        assert status_calls[-1] == ProjectStatus.COMPLETED

        # AI called once (one endpoint)
        mock_gen.assert_awaited_once()
        ai_payload = mock_gen.call_args[0][0]
        assert ai_payload["path"] == endpoint.path

        # Examples and doc page created
        mock_ex.assert_awaited_once()
        mock_doc.assert_awaited_once()

        assert result is project

    @pytest.mark.asyncio
    async def test_generate_docs_wrong_status_raises_invalid_status_error(self):
        """
        generate_docs raises AppError(INVALID_STATUS) when the project is
        still PENDING — it must be PARSED or COMPLETED first.
        """
        project = _mock_project(status=ProjectStatus.PENDING)
        db = AsyncMock()

        with patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)):
            with pytest.raises(AppError) as exc_info:
                await project_service.generate_docs(db, project.id, "cuid_user_1")

        assert exc_info.value.code == "INVALID_STATUS"
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_generate_docs_ai_error_sets_failed_status(self):
        """
        When ai_client.generate_docs raises AIServiceError, project status
        is set to FAILED and the exception is re-raised.
        """
        endpoint = _mock_endpoint()
        project = _mock_project(status=ProjectStatus.PARSED, endpoints=[endpoint])
        db = AsyncMock()
        status_calls: list[ProjectStatus] = []

        async def _capture_status(db, pid, status):
            status_calls.append(status)
            return project

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(side_effect=_capture_status)),
            patch(f"{_EP_CRUD}.get_endpoints_by_project", new=AsyncMock(return_value=[endpoint])),
            patch(f"{_AI}.generate_docs", new=AsyncMock(side_effect=AIServiceError("Groq down"))),
        ):
            with pytest.raises(AIServiceError, match="Groq down"):
                await project_service.generate_docs(db, project.id, "cuid_user_1")

        assert ProjectStatus.FAILED in status_calls

    @pytest.mark.asyncio
    async def test_generate_docs_skips_examples_when_ai_returns_none(self):
        """
        When the AI returns no examples (empty list), add_examples_to_endpoint
        is NOT called — avoiding empty batch inserts.
        """
        endpoint = _mock_endpoint()
        project = _mock_project(status=ProjectStatus.PARSED)
        db = AsyncMock()

        ai_response_no_examples = {
            "examples": [],
            "documentation": {
                "title": "GET /users",
                "content": "Docs content",
            },
        }

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(return_value=project)),
            patch(f"{_EP_CRUD}.get_endpoints_by_project", new=AsyncMock(return_value=[endpoint])),
            patch(f"{_AI}.generate_docs", new=AsyncMock(return_value=ai_response_no_examples)),
            patch(f"{_EP_CRUD}.add_examples_to_endpoint", new=AsyncMock()) as mock_ex,
            patch(f"{_DOC_CRUD}.create_doc_page", new=AsyncMock(return_value=MagicMock())),
        ):
            await project_service.generate_docs(db, project.id, "cuid_user_1")

        mock_ex.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_docs_skips_doc_page_when_ai_returns_no_documentation(self):
        """
        When the AI returns no 'documentation' key, create_doc_page is NOT
        called — preventing empty doc page rows.
        """
        endpoint = _mock_endpoint()
        project = _mock_project(status=ProjectStatus.PARSED)
        db = AsyncMock()

        ai_response_no_docs = {
            "examples": [
                {"type": "RESPONSE", "title": "OK", "language": "json",
                 "code": "{}", "status_code": 200}
            ],
            "documentation": None,
        }

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(return_value=project)),
            patch(f"{_EP_CRUD}.get_endpoints_by_project", new=AsyncMock(return_value=[endpoint])),
            patch(f"{_AI}.generate_docs", new=AsyncMock(return_value=ai_response_no_docs)),
            patch(f"{_EP_CRUD}.add_examples_to_endpoint", new=AsyncMock(return_value=[])),
            patch(f"{_DOC_CRUD}.create_doc_page", new=AsyncMock()) as mock_doc,
        ):
            await project_service.generate_docs(db, project.id, "cuid_user_1")

        mock_doc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_docs_also_accepts_completed_status(self):
        """
        generate_docs accepts COMPLETED status (re-generation) in addition
        to PARSED, per the service-layer condition check.
        """
        endpoint = _mock_endpoint()
        project = _mock_project(status=ProjectStatus.COMPLETED)
        db = AsyncMock()

        with (
            patch(f"{_PROJ_CRUD}.get_project_by_id", new=AsyncMock(return_value=project)),
            patch(f"{_PROJ_CRUD}.update_project_status", new=AsyncMock(return_value=project)),
            patch(f"{_EP_CRUD}.get_endpoints_by_project", new=AsyncMock(return_value=[endpoint])),
            patch(f"{_AI}.generate_docs", new=AsyncMock(return_value={"examples": [], "documentation": None})),
            patch(f"{_EP_CRUD}.add_examples_to_endpoint", new=AsyncMock(return_value=[])),
            patch(f"{_DOC_CRUD}.create_doc_page", new=AsyncMock()),
        ):
            # Should NOT raise INVALID_STATUS
            await project_service.generate_docs(db, project.id, "cuid_user_1")
