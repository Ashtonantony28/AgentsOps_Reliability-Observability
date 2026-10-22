from datetime import datetime
from typing import Literal, Optional, List
from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = ""
    org_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    created_at: datetime

    class Config:
        from_attributes = True


class OrgOut(BaseModel):
    id: str
    name: str
    slug: str
    tier: str
    retention_days: int

    class Config:
        from_attributes = True


class MemberOut(BaseModel):
    user_id: str
    email: str
    full_name: str
    role: str


class InviteRequest(BaseModel):
    email: EmailStr
    role: Literal["viewer", "developer", "admin"] = "developer"


class InviteOut(BaseModel):
    """`POST /api/auth/invite` from SG-P1 on: never a password or token (the token goes to the invitee by email)."""
    email: str
    role: str
    status: Literal["added", "pending"]  # added: the email has an account and is now a member; pending: invite sent
    invite_id: Optional[str]  # pending only
    expires_at: Optional[datetime]  # pending only


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: Optional[datetime]
    revoked_at: Optional[datetime]
    key: Optional[str] = None  # full key only returned on creation

    class Config:
        from_attributes = True


class SessionOut(BaseModel):
    id: str
    user_agent: str
    ip_address: str
    created_at: datetime
    expires_at: datetime

    class Config:
        from_attributes = True


class MeResponse(BaseModel):
    user: UserOut
    organizations: List[OrgOut]
    role: str
    active_org_id: str
