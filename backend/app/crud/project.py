from __future__ import annotations

from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.project import Project
from app.models.endpoint import Endpoint
from app.models.enums import ProjectStatus


async def create_project(db: AsyncSession, **kwargs) -> Project:
    project = Project(**kwargs)
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return project


async def get_project_by_id(db: AsyncSession, project_id: str) -> Project | None:
    stmt = (
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.endpoints).selectinload(Endpoint.parameters),
            selectinload(Project.endpoints).selectinload(Endpoint.examples),
            selectinload(Project.documentation_pages),
        )
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_projects(
    db: AsyncSession,
    user_id: str,
    page: int = 1,
    limit: int = 20,
    status: str | None = None,
) -> tuple[list[Project], int]:
    where_clause = [Project.user_id == user_id]
    if status:
        where_clause.append(Project.status == status)

    # Count
    count_stmt = select(func.count(Project.id))
    if where_clause:
        count_stmt = count_stmt.where(*where_clause)
    total = (await db.execute(count_stmt)).scalar_one()

    # Data with endpoint count
    data_stmt = (
        select(Project)
        .order_by(Project.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    if where_clause:
        data_stmt = data_stmt.where(*where_clause)

    result = await db.execute(data_stmt)
    projects = list(result.scalars().all())

    # Attach endpoint counts efficiently
    if projects:
        ids = [p.id for p in projects]
        count_stmt = (
            select(Endpoint.project_id, func.count(Endpoint.id))
            .where(Endpoint.project_id.in_(ids))
            .group_by(Endpoint.project_id)
        )
        counts = dict((await db.execute(count_stmt)).all())
        for p in projects:
            p.endpoint_count = counts.get(p.id, 0)  # type: ignore[attr-defined]

    return projects, total


async def update_project_name(
    db: AsyncSession, project_id: str, name: str
) -> Project | None:
    project = await db.get(Project, project_id)
    if project:
        project.name = name
        await db.flush()
        await db.refresh(project)
    return project


async def update_project_status(
    db: AsyncSession, project_id: str, status: ProjectStatus
) -> Project | None:
    project = await db.get(Project, project_id)
    if project:
        project.status = status
        await db.flush()
        await db.refresh(project)
    return project


async def delete_project(db: AsyncSession, project_id: str) -> bool:
    stmt = sa_delete(Project).where(Project.id == project_id)
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0  # type: ignore[union-attr]
