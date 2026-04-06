from __future__ import annotations

import math
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectOut, ProjectListItem, ProjectIdBody, ProjectRename
from app.schemas.endpoint import EndpointOut
from app.schemas.response import SuccessResponse, PaginatedResponse, PaginationMeta
from app.services import project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=SuccessResponse[ProjectOut], status_code=201)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await project_service.create_project(
        db,
        user_id=current_user.id,
        name=body.name,
        description=body.description,
        source_type=body.source_type,
        source_code=body.source_code,
        framework=body.framework,
    )
    return SuccessResponse(data=ProjectOut.model_validate(project))


@router.get("", response_model=PaginatedResponse[ProjectListItem])
async def list_projects(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    projects, total = await project_service.list_projects(
        db, user_id=current_user.id, page=page, limit=limit, status=status
    )
    items = []
    for p in projects:
        item = ProjectListItem.model_validate(p)
        item.endpoint_count = getattr(p, "endpoint_count", 0)
        items.append(item)

    return PaginatedResponse(
        data=items,
        pagination=PaginationMeta(
            page=page,
            limit=limit,
            total=total,
            total_pages=math.ceil(total / limit) if limit else 0,
        ),
    )


@router.get("/{project_id}", response_model=SuccessResponse[ProjectOut])
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await project_service.get_project(db, project_id, current_user.id)
    return SuccessResponse(data=ProjectOut.model_validate(project))


@router.patch("/{project_id}", response_model=SuccessResponse[ProjectOut])
async def rename_project(
    project_id: str,
    body: ProjectRename,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await project_service.rename_project(db, project_id, body.name, current_user.id)
    return SuccessResponse(data=ProjectOut.model_validate(project))


@router.delete("/{project_id}", response_model=SuccessResponse[dict])
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await project_service.delete_project(db, project_id, current_user.id)
    return SuccessResponse(data={"deleted": True})


@router.post("/parse-api", response_model=SuccessResponse[list[EndpointOut]])
async def parse_api(
    body: ProjectIdBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    endpoints = await project_service.parse_api(db, body.project_id, current_user.id)
    return SuccessResponse(
        data=[EndpointOut.model_validate(ep) for ep in endpoints]
    )


@router.post("/generate-docs", response_model=SuccessResponse[ProjectOut])
async def generate_docs(
    body: ProjectIdBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await project_service.generate_docs(db, body.project_id, current_user.id)
    return SuccessResponse(data=ProjectOut.model_validate(project))
