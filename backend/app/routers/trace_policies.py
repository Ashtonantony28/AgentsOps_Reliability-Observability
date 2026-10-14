from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, authenticate_api_key, get_current_user, require_role
from app.schemas.policy import (
    PolicyCheckIn,
    PolicyCheckOut,
    PolicyValidateIn,
    PolicyValidateOut,
    TracePolicyCreate,
    TracePolicyOut,
    TracePolicyUpdate,
)

router = APIRouter(prefix="/api/trace-policies", tags=["trace-policies"])


@router.get("", response_model=List[TracePolicyOut])
async def list_trace_policies(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    raise HTTPException(501, "not_implemented")


@router.post("", response_model=TracePolicyOut)
async def create_trace_policy(
    req: TracePolicyCreate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    raise HTTPException(501, "not_implemented")


@router.post("/validate", response_model=PolicyValidateOut)
async def validate_trace_policy(
    req: PolicyValidateIn,
    ctx: AuthContext = Depends(get_current_user),
):
    raise HTTPException(501, "not_implemented")


@router.post("/check", response_model=PolicyCheckOut)
async def check_candidate(req: PolicyCheckIn, request: Request, db: AsyncSession = Depends(get_db)):
    org, _api_key = await authenticate_api_key(request, db)
    raise HTTPException(501, "not_implemented")


@router.put("/{policy_id}", response_model=TracePolicyOut)
async def update_trace_policy(
    policy_id: str,
    req: TracePolicyUpdate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    raise HTTPException(501, "not_implemented")


@router.delete("/{policy_id}")
async def delete_trace_policy(
    policy_id: str,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    raise HTTPException(501, "not_implemented")
