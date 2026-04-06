"""
tests/test_schemas.py — Pydantic schema validation tests.

Coverage:
- ProjectCreate: field constraints (min/max length, defaults)
- ProjectIdBody: camelCase alias acceptance and snake_case access
- ProjectOut / ProjectListItem: ORM-mode (from_attributes) round-trip
- ProjectRename: boundary length validation
- UserCreate: custom field_validators (name whitespace, password length)
- UserLogin / UserOut / TokenResponse: structural correctness
- EndpointOut / ParameterOut / ExampleOut: nested from_attributes chains
- DocPageOut: full field mapping
- SuccessResponse / PaginatedResponse / ErrorResponse: generic wrappers
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.schemas.auth import TokenResponse, UserCreate, UserLogin, UserOut
from app.schemas.doc_page import DocPageOut
from app.schemas.endpoint import EndpointCreate, EndpointOut
from app.schemas.example import ExampleCreate, ExampleOut
from app.schemas.parameter import ParameterCreate, ParameterOut
from app.schemas.project import (
    ProjectCreate,
    ProjectIdBody,
    ProjectListItem,
    ProjectOut,
    ProjectRename,
)
from app.schemas.response import (
    ErrorDetail,
    ErrorResponse,
    PaginatedResponse,
    PaginationMeta,
    SuccessResponse,
)


# ── helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _orm(fields: dict[str, Any]) -> MagicMock:
    """Build a MagicMock that behaves like a SQLAlchemy ORM row."""
    obj = MagicMock()
    for k, v in fields.items():
        setattr(obj, k, v)
    return obj


# ══════════════════════════════════════════════════════════════════════════════
# ProjectCreate
# ══════════════════════════════════════════════════════════════════════════════

class TestProjectCreate:
    def test_valid_payload_accepted(self):
        """A fully valid ProjectCreate payload is accepted without errors."""
        p = ProjectCreate(
            name="SpecForge Demo",
            description="AI doc generator",
            source_type="RAW_CODE",
            source_code="from fastapi import FastAPI\napp = FastAPI()",
            framework="fastapi",
        )
        assert p.name == "SpecForge Demo"
        assert p.framework == "fastapi"
        assert p.source_type == "RAW_CODE"

    def test_defaults_applied_when_optional_fields_omitted(self):
        """source_type defaults to RAW_CODE; description and framework are None."""
        p = ProjectCreate(
            name="Minimal",
            source_code="x" * 10,
        )
        assert p.source_type == "RAW_CODE"
        assert p.description is None
        assert p.framework is None

    def test_name_too_short_raises_validation_error(self):
        """Empty name (min_length=1) must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ProjectCreate(name="", source_code="x" * 10)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("name",) for e in errors)

    def test_name_too_long_raises_validation_error(self):
        """Name longer than 100 chars must raise ValidationError."""
        with pytest.raises(ValidationError):
            ProjectCreate(name="x" * 101, source_code="x" * 10)

    def test_source_code_below_minimum_length_rejected(self):
        """source_code shorter than 10 chars must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ProjectCreate(name="Test", source_code="tiny")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("source_code",) for e in errors)

    def test_source_code_above_maximum_length_rejected(self):
        """source_code longer than 100 000 chars must raise ValidationError."""
        with pytest.raises(ValidationError):
            ProjectCreate(name="Test", source_code="x" * 100_001)

    def test_description_above_maximum_length_rejected(self):
        """description longer than 500 chars must raise ValidationError."""
        with pytest.raises(ValidationError):
            ProjectCreate(
                name="Test",
                source_code="x" * 10,
                description="d" * 501,
            )

    def test_missing_required_field_raises_validation_error(self):
        """Omitting source_code (required) must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ProjectCreate(name="Test")  # type: ignore[call-arg]
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("source_code",) for e in errors)


# ══════════════════════════════════════════════════════════════════════════════
# ProjectIdBody — alias handling
# ══════════════════════════════════════════════════════════════════════════════

class TestProjectIdBody:
    def test_camel_case_alias_accepted(self):
        """ProjectIdBody accepts camelCase 'projectId' from JSON/dict input."""
        body = ProjectIdBody.model_validate({"projectId": "cuid_abc123"})
        assert body.project_id == "cuid_abc123"

    def test_snake_case_field_name_also_accepted(self):
        """populate_by_name=True allows snake_case 'project_id' as well."""
        body = ProjectIdBody(project_id="cuid_snake")
        assert body.project_id == "cuid_snake"

    def test_missing_project_id_raises_validation_error(self):
        """Omitting both alias and field name must raise ValidationError."""
        with pytest.raises(ValidationError):
            ProjectIdBody.model_validate({})

    def test_serialization_uses_alias_by_default(self):
        """model_dump(by_alias=True) produces camelCase key 'projectId'."""
        body = ProjectIdBody(project_id="cuid_xyz")
        dumped = body.model_dump(by_alias=True)
        assert "projectId" in dumped
        assert dumped["projectId"] == "cuid_xyz"


# ══════════════════════════════════════════════════════════════════════════════
# ProjectRename
# ══════════════════════════════════════════════════════════════════════════════

class TestProjectRename:
    def test_valid_name_accepted(self):
        """A name within 1–100 chars is accepted."""
        r = ProjectRename(name="New Name")
        assert r.name == "New Name"

    def test_empty_name_rejected(self):
        """An empty string must not pass min_length=1."""
        with pytest.raises(ValidationError):
            ProjectRename(name="")

    def test_name_at_maximum_boundary_accepted(self):
        """A name of exactly 100 chars sits on the valid boundary."""
        r = ProjectRename(name="a" * 100)
        assert len(r.name) == 100

    def test_name_exceeds_maximum_boundary_rejected(self):
        """101 chars must fail max_length=100."""
        with pytest.raises(ValidationError):
            ProjectRename(name="a" * 101)


# ══════════════════════════════════════════════════════════════════════════════
# ProjectOut — from_attributes (ORM mode)
# ══════════════════════════════════════════════════════════════════════════════

class TestProjectOut:
    def _make_orm_project(self, **overrides) -> MagicMock:
        defaults = {
            "id": "cuid_proj_1",
            "name": "Demo API",
            "description": "Test project",
            "source_type": "RAW_CODE",
            "source_code": "code here",
            "framework": "fastapi",
            "status": "PENDING",
            "created_at": _NOW,
            "updated_at": _NOW,
            "endpoints": [],
            "documentation_pages": [],
        }
        return _orm({**defaults, **overrides})

    def test_orm_object_converts_to_schema(self):
        """ProjectOut.model_validate() converts an ORM-like object successfully."""
        orm_obj = self._make_orm_project()
        out = ProjectOut.model_validate(orm_obj)
        assert out.id == "cuid_proj_1"
        assert out.name == "Demo API"
        assert out.status == "PENDING"
        assert out.endpoints == []

    def test_optional_fields_preserved_as_none(self):
        """description=None and framework=None are forwarded without errors."""
        orm_obj = self._make_orm_project(description=None, framework=None)
        out = ProjectOut.model_validate(orm_obj)
        assert out.description is None
        assert out.framework is None

    def test_datetime_fields_present(self):
        """created_at and updated_at are serialized from ORM datetime objects."""
        orm_obj = self._make_orm_project()
        out = ProjectOut.model_validate(orm_obj)
        assert isinstance(out.created_at, datetime)
        assert isinstance(out.updated_at, datetime)


# ══════════════════════════════════════════════════════════════════════════════
# ProjectListItem — endpoint_count field
# ══════════════════════════════════════════════════════════════════════════════

class TestProjectListItem:
    def test_endpoint_count_defaults_to_zero(self):
        """When endpoint_count is not on the ORM object it defaults to 0."""
        orm_obj = _orm({
            "id": "cuid2",
            "name": "API",
            "description": None,
            "source_type": "RAW_CODE",
            "framework": None,
            "status": "COMPLETED",
            "created_at": _NOW,
            "updated_at": _NOW,
            "endpoint_count": 0,
        })
        item = ProjectListItem.model_validate(orm_obj)
        assert item.endpoint_count == 0

    def test_endpoint_count_reflects_orm_attribute(self):
        """endpoint_count set on the ORM object is preserved in the schema."""
        orm_obj = _orm({
            "id": "cuid3",
            "name": "API",
            "description": None,
            "source_type": "RAW_CODE",
            "framework": None,
            "status": "PARSED",
            "created_at": _NOW,
            "updated_at": _NOW,
            "endpoint_count": 7,
        })
        item = ProjectListItem.model_validate(orm_obj)
        assert item.endpoint_count == 7


# ══════════════════════════════════════════════════════════════════════════════
# UserCreate — custom validators
# ══════════════════════════════════════════════════════════════════════════════

class TestUserCreate:
    def test_valid_user_accepted(self):
        """A fully valid UserCreate payload is accepted."""
        u = UserCreate(
            name="Alice Wonderland",
            email="alice@example.com",
            password="supersecret1",
        )
        assert u.name == "Alice Wonderland"
        assert u.email == "alice@example.com"

    def test_name_whitespace_only_raises_validation_error(self):
        """name_not_empty validator strips and rejects whitespace-only names."""
        with pytest.raises(ValidationError) as exc_info:
            UserCreate(name="   ", email="a@b.com", password="password123")
        assert any("Name cannot be empty" in str(e) for e in exc_info.value.errors())

    def test_name_is_stripped_of_surrounding_whitespace(self):
        """name_not_empty validator strips surrounding whitespace from valid names."""
        u = UserCreate(name="  Bob  ", email="bob@example.com", password="password123")
        assert u.name == "Bob"

    def test_password_below_8_chars_rejected(self):
        """password_min_length validator rejects passwords shorter than 8 chars."""
        with pytest.raises(ValidationError) as exc_info:
            UserCreate(name="Carol", email="c@d.com", password="short")
        assert any("8 characters" in str(e) for e in exc_info.value.errors())

    def test_password_exactly_8_chars_accepted(self):
        """8-character password sits on the minimum valid boundary."""
        u = UserCreate(name="Dave", email="d@e.com", password="12345678")
        assert len(u.password) == 8

    def test_invalid_email_rejected(self):
        """EmailStr rejects a syntactically invalid email address."""
        with pytest.raises(ValidationError):
            UserCreate(name="Eve", email="not-an-email", password="password123")

    def test_missing_email_raises_validation_error(self):
        """Omitting email raises ValidationError."""
        with pytest.raises(ValidationError):
            UserCreate(name="Frank", password="password123")  # type: ignore[call-arg]


# ══════════════════════════════════════════════════════════════════════════════
# UserOut / TokenResponse — ORM mode
# ══════════════════════════════════════════════════════════════════════════════

class TestUserOut:
    def test_orm_object_converts_to_user_out(self):
        """UserOut.model_validate() maps ORM attributes correctly."""
        orm_user = _orm({
            "id": "cuid_user_1",
            "name": "Grace",
            "email": "grace@example.com",
            "created_at": _NOW,
        })
        out = UserOut.model_validate(orm_user)
        assert out.id == "cuid_user_1"
        assert out.name == "Grace"
        assert out.email == "grace@example.com"


class TestTokenResponse:
    def test_default_message_present(self):
        """TokenResponse includes the default 'Authentication successful' message."""
        user_out = UserOut(
            id="cuid_u2",
            name="Heidi",
            email="heidi@example.com",
            created_at=_NOW,
        )
        token = TokenResponse(user=user_out)
        assert token.message == "Authentication successful"
        assert token.user.name == "Heidi"

    def test_custom_message_overrides_default(self):
        """A custom message replaces the default when explicitly provided."""
        user_out = UserOut(
            id="cuid_u3",
            name="Ivan",
            email="ivan@example.com",
            created_at=_NOW,
        )
        token = TokenResponse(user=user_out, message="Registered successfully")
        assert token.message == "Registered successfully"


# ══════════════════════════════════════════════════════════════════════════════
# ParameterOut / ExampleOut / EndpointOut — nested ORM chains
# ══════════════════════════════════════════════════════════════════════════════

class TestParameterOut:
    def test_parameter_orm_conversion(self):
        """ParameterOut maps all ORM fields including optional schema_def."""
        orm_p = _orm({
            "id": "cuid_p1",
            "endpoint_id": "cuid_ep1",
            "name": "page",
            "location": "QUERY",
            "type": "integer",
            "required": False,
            "description": "Page number",
            "schema_def": {"type": "integer", "minimum": 1},
            "created_at": _NOW,
        })
        out = ParameterOut.model_validate(orm_p)
        assert out.name == "page"
        assert out.location == "QUERY"
        assert out.schema_def == {"type": "integer", "minimum": 1}

    def test_parameter_optional_fields_nullable(self):
        """description and schema_def may be None without errors."""
        orm_p = _orm({
            "id": "cuid_p2",
            "endpoint_id": "cuid_ep1",
            "name": "Authorization",
            "location": "HEADER",
            "type": "string",
            "required": True,
            "description": None,
            "schema_def": None,
            "created_at": _NOW,
        })
        out = ParameterOut.model_validate(orm_p)
        assert out.description is None
        assert out.schema_def is None


class TestExampleOut:
    def test_example_orm_conversion(self):
        """ExampleOut maps all ORM fields including optional status_code."""
        orm_ex = _orm({
            "id": "cuid_ex1",
            "endpoint_id": "cuid_ep1",
            "type": "RESPONSE",
            "title": "200 OK",
            "language": "json",
            "code": '{"success": true}',
            "status_code": 200,
            "created_at": _NOW,
        })
        out = ExampleOut.model_validate(orm_ex)
        assert out.type == "RESPONSE"
        assert out.status_code == 200

    def test_status_code_may_be_none_for_request_examples(self):
        """REQUEST examples typically have no status_code — None is valid."""
        orm_ex = _orm({
            "id": "cuid_ex2",
            "endpoint_id": "cuid_ep1",
            "type": "REQUEST",
            "title": "Sample request",
            "language": "http",
            "code": "GET /users HTTP/1.1",
            "status_code": None,
            "created_at": _NOW,
        })
        out = ExampleOut.model_validate(orm_ex)
        assert out.status_code is None


class TestEndpointOut:
    def test_endpoint_out_with_nested_parameters_and_examples(self):
        """
        EndpointOut from_attributes correctly includes nested parameter
        and example sub-schemas.
        """
        param_orm = _orm({
            "id": "cuid_p3",
            "endpoint_id": "cuid_ep3",
            "name": "limit",
            "location": "QUERY",
            "type": "integer",
            "required": False,
            "description": None,
            "schema_def": None,
            "created_at": _NOW,
        })
        example_orm = _orm({
            "id": "cuid_ex3",
            "endpoint_id": "cuid_ep3",
            "type": "RESPONSE",
            "title": "Success",
            "language": "json",
            "code": "{}",
            "status_code": 200,
            "created_at": _NOW,
        })
        endpoint_orm = _orm({
            "id": "cuid_ep3",
            "project_id": "cuid_proj1",
            "path": "/items",
            "method": "GET",
            "summary": "List items",
            "description": "Returns paginated items",
            "tag": "Items",
            "created_at": _NOW,
            "updated_at": _NOW,
            "parameters": [param_orm],
            "examples": [example_orm],
        })
        out = EndpointOut.model_validate(endpoint_orm)
        assert out.path == "/items"
        assert out.method == "GET"
        assert len(out.parameters) == 1
        assert out.parameters[0].name == "limit"
        assert len(out.examples) == 1
        assert out.examples[0].status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# DocPageOut
# ══════════════════════════════════════════════════════════════════════════════

class TestDocPageOut:
    def test_doc_page_orm_conversion(self):
        """DocPageOut maps all fields including order and slug."""
        orm_doc = _orm({
            "id": "cuid_doc1",
            "project_id": "cuid_proj1",
            "title": "Authentication",
            "slug": "authentication",
            "content": "## Auth\n\nUse Bearer tokens.",
            "order": 0,
            "created_at": _NOW,
            "updated_at": _NOW,
        })
        out = DocPageOut.model_validate(orm_doc)
        assert out.slug == "authentication"
        assert out.order == 0


# ══════════════════════════════════════════════════════════════════════════════
# Generic response wrappers
# ══════════════════════════════════════════════════════════════════════════════

class TestSuccessResponse:
    def test_success_response_wraps_arbitrary_data(self):
        """SuccessResponse[dict] sets success=True and embeds data."""
        resp: SuccessResponse[dict] = SuccessResponse(data={"key": "value"})
        assert resp.success is True
        assert resp.data == {"key": "value"}

    def test_success_response_wraps_list(self):
        """SuccessResponse[list] correctly wraps a list payload."""
        resp: SuccessResponse[list] = SuccessResponse(data=[1, 2, 3])
        assert resp.data == [1, 2, 3]


class TestPaginatedResponse:
    def test_paginated_response_includes_pagination_meta(self):
        """PaginatedResponse embeds a correctly populated PaginationMeta."""
        resp: PaginatedResponse[str] = PaginatedResponse(
            data=["a", "b"],
            pagination=PaginationMeta(page=2, limit=10, total=25, total_pages=3),
        )
        assert resp.success is True
        assert len(resp.data) == 2
        assert resp.pagination.total == 25
        assert resp.pagination.total_pages == 3

    def test_pagination_meta_fields(self):
        """PaginationMeta correctly stores page, limit, total, total_pages."""
        meta = PaginationMeta(page=1, limit=20, total=0, total_pages=0)
        assert meta.page == 1
        assert meta.limit == 20


class TestErrorResponse:
    def test_error_response_structure(self):
        """ErrorResponse sets success=False and includes code + message."""
        err = ErrorResponse(
            error=ErrorDetail(
                code="NOT_FOUND",
                message="Project not found",
                details=None,
            )
        )
        assert err.success is False
        assert err.error.code == "NOT_FOUND"
        assert err.error.message == "Project not found"

    def test_error_detail_with_list_details(self):
        """ErrorDetail.details can hold a list of validation problem objects."""
        detail = ErrorDetail(
            code="VALIDATION_ERROR",
            message="Request validation failed",
            details=[{"field": "name", "msg": "too short"}],
        )
        assert detail.details[0]["field"] == "name"


# ══════════════════════════════════════════════════════════════════════════════
# EndpointCreate / ParameterCreate / ExampleCreate — creation schemas
# ══════════════════════════════════════════════════════════════════════════════

class TestEndpointCreate:
    def test_endpoint_create_with_nested_parameters(self):
        """EndpointCreate accepts a list of ParameterCreate sub-schemas."""
        ep = EndpointCreate(
            path="/projects",
            method="POST",
            summary="Create project",
            description=None,
            tag="Projects",
            parameters=[
                ParameterCreate(
                    name="Authorization",
                    location="HEADER",
                    type="string",
                    required=True,
                    description="Bearer token",
                    schema_def=None,
                )
            ],
            examples=[],
        )
        assert ep.path == "/projects"
        assert len(ep.parameters) == 1
        assert ep.parameters[0].required is True

    def test_parameter_create_type_defaults_to_string(self):
        """ParameterCreate.type defaults to 'string' when not specified."""
        p = ParameterCreate(name="q", location="QUERY")
        assert p.type == "string"
        assert p.required is False

    def test_example_create_accepts_all_example_types(self):
        """ExampleCreate accepts REQUEST, RESPONSE, and ERROR types."""
        for ex_type in ("REQUEST", "RESPONSE", "ERROR"):
            ex = ExampleCreate(
                type=ex_type,
                title=f"{ex_type} example",
                language="json",
                code="{}",
                status_code=None,
            )
            assert ex.type == ex_type
