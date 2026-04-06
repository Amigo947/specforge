from __future__ import annotations

import re
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, AppError
from app.core.logging import logger
from app.crud import project as project_crud
from app.crud import endpoint as endpoint_crud
from app.crud import doc_page as doc_page_crud
from app.models.enums import ProjectStatus
from app.schemas.endpoint import EndpointCreate
from app.schemas.parameter import ParameterCreate
from app.schemas.example import ExampleCreate
from app.services import ai_client


async def create_project(db: AsyncSession, **kwargs):
    return await project_crud.create_project(db, **kwargs)


async def get_project(db: AsyncSession, project_id: str, user_id: str):
    project = await project_crud.get_project_by_id(db, project_id)
    if not project:
        raise NotFoundError("Project")
    if project.user_id != user_id:
        raise NotFoundError("Project")
    return project


async def list_projects(
    db: AsyncSession,
    user_id: str,
    page: int = 1,
    limit: int = 20,
    status: str | None = None,
):
    return await project_crud.list_projects(db, user_id=user_id, page=page, limit=limit, status=status)


async def rename_project(db: AsyncSession, project_id: str, name: str, user_id: str):
    await get_project(db, project_id, user_id)
    return await project_crud.update_project_name(db, project_id, name)


async def delete_project(db: AsyncSession, project_id: str, user_id: str):
    await get_project(db, project_id, user_id)
    return await project_crud.delete_project(db, project_id)


async def parse_api(db: AsyncSession, project_id: str, user_id: str):
    project = await get_project(db, project_id, user_id)

    await project_crud.update_project_status(db, project_id, ProjectStatus.PARSING)

    try:
        parsed = await ai_client.parse_api(
            source_code=project.source_code,
            source_type=project.source_type.value,
            framework=project.framework,
        )

        raw_endpoints = parsed.get("endpoints")
        if not isinstance(raw_endpoints, list):
            raise AppError(
                "AI returned invalid parsing result", 502, "AI_INVALID_RESPONSE"
            )

        # Map AI response → EndpointCreate schemas
        endpoint_creates: list[EndpointCreate] = []
        for ep in raw_endpoints:
            params = [
                ParameterCreate(
                    name=p.get("name", "unknown"),
                    location=p.get("location", "QUERY").upper(),
                    type=p.get("type", "string"),
                    required=bool(p.get("required", False)),
                    description=p.get("description"),
                    schema_def=p.get("schema"),
                )
                for p in (ep.get("parameters") or [])
            ]
            endpoint_creates.append(
                EndpointCreate(
                    path=ep["path"],
                    method=ep["method"].upper(),
                    summary=ep.get("summary"),
                    description=ep.get("description"),
                    tag=ep.get("tag"),
                    parameters=params,
                    examples=[],
                )
            )

        created = await endpoint_crud.create_endpoints_batch(
            db, project_id, endpoint_creates
        )
        await project_crud.update_project_status(db, project_id, ProjectStatus.PARSED)

        logger.info(
            "API parsed successfully: project_id=%s endpoint_count=%d",
            project_id, len(created),
        )
        return created

    except Exception:
        await project_crud.update_project_status(db, project_id, ProjectStatus.FAILED)
        raise


async def generate_docs(db: AsyncSession, project_id: str, user_id: str):
    project = await get_project(db, project_id, user_id)

    if project.status not in (ProjectStatus.PARSED, ProjectStatus.COMPLETED):
        raise AppError(
            "Project must be parsed before generating docs", 400, "INVALID_STATUS"
        )

    await project_crud.update_project_status(
        db, project_id, ProjectStatus.GENERATING
    )

    try:
        endpoints = await endpoint_crud.get_endpoints_by_project(db, project_id)

        for idx, endpoint in enumerate(endpoints):
            # Build payload for AI service
            params_payload = [
                {
                    "name": p.name,
                    "location": p.location.value if hasattr(p.location, "value") else p.location,
                    "type": p.type,
                    "required": p.required,
                    "description": p.description,
                }
                for p in endpoint.parameters
            ]

            docs = await ai_client.generate_docs(
                {
                    "path": endpoint.path,
                    "method": endpoint.method.value if hasattr(endpoint.method, "value") else endpoint.method,
                    "summary": endpoint.summary,
                    "description": endpoint.description,
                    "parameters": params_payload,
                }
            )

            # Save generated examples
            raw_examples = docs.get("examples") or []
            if raw_examples:
                example_creates = [
                    ExampleCreate(
                        type=ex.get("type", "RESPONSE"),
                        title=ex.get("title", "Example"),
                        language=ex.get("language"),
                        code=ex.get("code", ""),
                        status_code=ex.get("status_code"),
                    )
                    for ex in raw_examples
                ]
                await endpoint_crud.add_examples_to_endpoint(
                    db, endpoint.id, example_creates
                )

            # Save documentation page
            raw_doc = docs.get("documentation")
            if raw_doc:
                method_str = endpoint.method.value if hasattr(endpoint.method, "value") else endpoint.method
                slug_raw = f"{method_str.lower()}-{endpoint.path}"
                slug = re.sub(r"[^a-z0-9]+", "-", slug_raw, flags=re.IGNORECASE).strip("-")

                await doc_page_crud.create_doc_page(
                    db,
                    project_id=project_id,
                    title=raw_doc.get("title", f"{method_str} {endpoint.path}"),
                    slug=slug,
                    content=raw_doc.get("content", ""),
                    order=idx,
                )

        await project_crud.update_project_status(
            db, project_id, ProjectStatus.COMPLETED
        )
        logger.info("Documentation generated: project_id=%s", project_id)

        return await get_project(db, project_id, user_id)

    except Exception:
        await project_crud.update_project_status(db, project_id, ProjectStatus.FAILED)
        raise
