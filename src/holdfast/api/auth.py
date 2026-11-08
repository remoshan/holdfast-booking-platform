"""Accounts and tokens: register, log in, refresh and log out."""

from typing import Annotated, Literal, Self

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field, StringConstraints

from holdfast.api.dependencies import AppSettings, DatabaseSession
from holdfast.api.schemas import Email, RequestSchema
from holdfast.api.users import UserResponse
from holdfast.services import auth as service
from holdfast.services.auth import IssuedTokens

router = APIRouter(prefix="/auth", tags=["auth"])

# OWASP ASVS V2.1: at least 12 characters, and at least 64 allowed. The cap bounds hashing work.
NewPassword = Annotated[str, StringConstraints(min_length=12, max_length=128)]


class RegistrationRequest(RequestSchema):
    email: Email
    password: NewPassword


class LoginRequest(RequestSchema):
    email: Email
    password: Annotated[str, StringConstraints(max_length=128)]


class RefreshTokenRequest(RequestSchema):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the OAuth 2.0 scheme name, not a secret
    expires_in: int = Field(description="Seconds until the access token expires.")
    refresh_token: str = Field(description="Exchange once at POST /auth/refresh for new tokens.")

    @classmethod
    def from_tokens(cls, tokens: IssuedTokens) -> Self:
        return cls(
            access_token=tokens.access_token,
            expires_in=int(tokens.access_token_lifetime.total_seconds()),
            refresh_token=tokens.refresh_token,
        )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegistrationRequest, session: DatabaseSession) -> UserResponse:
    return UserResponse.model_validate(await service.register(session, body.email, body.password))


@router.post("/login")
async def log_in(
    body: LoginRequest, session: DatabaseSession, settings: AppSettings, response: Response
) -> TokenResponse:
    tokens = await service.log_in(
        session, body.email, body.password, settings.jwt_secret.get_secret_value()
    )
    _forbid_caching(response)
    return TokenResponse.from_tokens(tokens)


@router.post("/refresh")
async def refresh(
    body: RefreshTokenRequest, session: DatabaseSession, settings: AppSettings, response: Response
) -> TokenResponse:
    """Exchange a refresh token for new tokens. Sending a used one again logs that sign-in out."""
    tokens = await service.refresh(
        session, body.refresh_token, settings.jwt_secret.get_secret_value()
    )
    _forbid_caching(response)
    return TokenResponse.from_tokens(tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def log_out(body: RefreshTokenRequest, session: DatabaseSession) -> None:
    """End the sign-in this refresh token belongs to. Issued access tokens expire on their own."""
    await service.log_out(session, body.refresh_token)


def _forbid_caching(response: Response) -> None:
    # RFC 6749 section 5.1: responses that carry tokens must not be stored by any cache.
    response.headers["Cache-Control"] = "no-store"
