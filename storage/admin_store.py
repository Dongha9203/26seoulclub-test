"""
운영자 계정 저장소.

1단계 지식DB(documents/qa_log, public 스키마)와 완전히 분리하기 위해
같은 Supabase 프로젝트 안에서도 별도 스키마(admin)를 사용합니다.
"""

from typing import Optional, Dict

from storage.supabase_store import get_connection, _with_conn

_CREATE_SCHEMA_SQL = "CREATE SCHEMA IF NOT EXISTS admin"

_CREATE_OPERATORS_SQL = """
CREATE TABLE IF NOT EXISTS admin.operators (
    id            SERIAL PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def initialize_admin_db(conn=None) -> None:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(_CREATE_SCHEMA_SQL)
            cur.execute(_CREATE_OPERATORS_SQL)
        c.commit()
    finally:
        if owns_conn:
            c.close()


def get_operator_by_email(email: str, conn=None) -> Optional[Dict]:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM admin.operators WHERE email = %s", (email,))
            row = cur.fetchone()
    finally:
        if owns_conn:
            c.close()
    return dict(row) if row else None


def create_operator(email: str, password_hash: str, conn=None) -> None:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO admin.operators (email, password_hash) VALUES (%s, %s)",
                (email, password_hash),
            )
        c.commit()
    finally:
        if owns_conn:
            c.close()


def update_password(email: str, new_password_hash: str, conn=None) -> bool:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE admin.operators SET password_hash = %s, updated_at = NOW() WHERE email = %s",
                (new_password_hash, email),
            )
            updated = cur.rowcount
        c.commit()
    finally:
        if owns_conn:
            c.close()
    return updated > 0
