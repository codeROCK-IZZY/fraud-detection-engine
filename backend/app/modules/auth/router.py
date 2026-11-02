from fastapi import APIRouter, Cookie, Request, Response

from app.api.deps import DB, CurrentUser
from app.core.config import get_settings
from app.core.errors import TooManyRequests, Unauthorized
from app.core.ratelimit import SlidingWindowLimiter
from app.modules.auth import service
from app.modules.auth.schemas import LoginRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "fde_refresh"
login_limiter = SlidingWindowLimiter(limit=get_settings().login_rate_limit_per_minute)


def _set_refresh_cookie(response: Response, raw: str) -> None:
    s = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=s.refresh_token_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="strict",
        path="/api/v1/auth",
    )


def _token_response(access: str) -> TokenResponse:
    return TokenResponse(access_token=access, expires_in=get_settings().access_token_minutes * 60)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, response: Response, db: DB) -> TokenResponse:
    client = request.client.host if request.client else "unknown"
    if not login_limiter.allow(client):
        raise TooManyRequests("Too many login attempts, try again in a minute")
    user = service.authenticate(db, body.email, body.password)
    access, refresh = service.issue_tokens(db, user)
    _set_refresh_cookie(response, refresh)
    return _token_response(access)


@router.post("/refresh", response_model=TokenResponse)
def refresh(response: Response, db: DB, fde_refresh: str | None = Cookie(default=None)) -> TokenResponse:
    if not fde_refresh:
        raise Unauthorized("Missing refresh token")
    _, access, new_raw = service.rotate_refresh_token(db, fde_refresh)
    _set_refresh_cookie(response, new_raw)
    return _token_response(access)


@router.post("/logout", status_code=204)
def logout(response: Response, db: DB, fde_refresh: str | None = Cookie(default=None)) -> Response:
    if fde_refresh:
        service.revoke_refresh_token(db, fde_refresh)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
