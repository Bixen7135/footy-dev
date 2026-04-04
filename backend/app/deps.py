from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from .config import get_settings
from .database import get_session
from .models import User, UserRole
from .security import decode_token

settings = get_settings()


def get_db_session(session: Session = Depends(get_session)) -> Session:
    return session


def _extract_access_token(request: Request) -> str | None:
    cookie_token = request.cookies.get(settings.access_cookie_name)
    if cookie_token:
        return cookie_token

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header.removeprefix("Bearer ").strip()
    return None


def extract_refresh_token(request: Request) -> str | None:
    return request.cookies.get(settings.refresh_cookie_name)


def get_current_user(
    request: Request,
    session: Session = Depends(get_db_session),
) -> User:
    token = _extract_access_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    payload = decode_token(token, expected_type="access")
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id_raw = payload.get("sub")
    if not user_id_raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token subject missing")

    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject") from exc

    user = session.exec(select(User).where(User.id == user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_optional_user(
    request: Request,
    session: Session = Depends(get_db_session),
) -> User | None:
    token = _extract_access_token(request)
    if not token:
        return None
    payload = decode_token(token, expected_type="access")
    if not payload:
        return None
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        return None
    return session.exec(select(User).where(User.id == user_id, User.is_active.is_(True))).first()


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return user
