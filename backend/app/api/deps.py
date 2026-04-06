from __future__ import annotations

from typing import Optional

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.user import get_user_by_id
from app.db.session import get_db
from app.models.user import User
from app.services.auth_service import decode_access_token


async def get_current_user(
    access_token: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = decode_access_token(access_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user
