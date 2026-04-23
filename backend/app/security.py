from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt

from .config import get_settings

settings = get_settings()
MAX_BCRYPT_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > MAX_BCRYPT_PASSWORD_BYTES:
        raise ValueError(f"Password is too long (max {MAX_BCRYPT_PASSWORD_BYTES} UTF-8 bytes)")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        password_bytes = password.encode("utf-8")
        if len(password_bytes) > MAX_BCRYPT_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(password_bytes, hashed.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def create_token(subject: str, expires_minutes: int, token_type: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: Optional[str] = None) -> Optional[dict[str, Any]]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None

    if expected_type and payload.get("type") != expected_type:
        return None
    return payload
