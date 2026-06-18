"""
4단계 운영 대시보드 인증.

비밀번호는 bcrypt로 해싱하고, 세션은 JWT로 관리합니다 (Vercel 서버리스는
인스턴스 간 메모리가 공유되지 않으므로 서버 측 세션 저장 방식은 쓰지 않습니다).
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Header, HTTPException

JWT_ALGORITHM = "HS256"
JWT_EXPIRES_MINUTES = 60 * 12  # 12시간


def _get_secret() -> str:
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret:
        raise EnvironmentError("JWT_SECRET_KEY 환경변수가 설정되지 않았습니다.")
    return secret


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRES_MINUTES),
    }
    return jwt.encode(payload, _get_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> str:
    """검증에 성공하면 email(sub)을 반환합니다. 실패 시 jwt 예외를 그대로 던집니다."""
    payload = jwt.decode(token, _get_secret(), algorithms=[JWT_ALGORITHM])
    return payload["sub"]


def get_current_operator(authorization: Optional[str] = Header(None)) -> str:
    """FastAPI 의존성: `Authorization: Bearer <token>` 헤더를 검증하고 운영자 이메일을 반환합니다."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    token = authorization[len("Bearer "):]
    try:
        return decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다. 다시 로그인해주세요.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")
