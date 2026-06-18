"""
미해결/불완전 답변 조치 상태 저장소.

qa_log는 INSERT-only 이벤트 로그로 유지하고(불변), 운영자가 수시로 바꾸는
가변 워크플로 상태는 별도 테이블에서 관리합니다.
"""

from typing import Dict, Optional

from storage.supabase_store import get_connection, _with_conn

_CREATE_ACTION_STATUS_SQL = """
CREATE TABLE IF NOT EXISTS action_status (
    log_id     TEXT PRIMARY KEY REFERENCES qa_log(log_id),
    status     TEXT NOT NULL DEFAULT '대기',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

VALID_STATUSES = {"대기", "처리중", "완료"}


def initialize_action_db(conn=None) -> None:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(_CREATE_ACTION_STATUS_SQL)
        c.commit()
    finally:
        if owns_conn:
            c.close()


def get_status(log_id: str, conn=None) -> str:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT status FROM action_status WHERE log_id = %s", (log_id,))
            row = cur.fetchone()
    finally:
        if owns_conn:
            c.close()
    return row["status"] if row else "대기"


def get_statuses(log_ids, conn=None) -> Dict[str, str]:
    """여러 log_id의 상태를 한 번에 조회합니다 (목록 화면용). 상태가 없으면 '대기'로 채웁니다."""
    log_ids = list(log_ids)
    result = {log_id: "대기" for log_id in log_ids}
    if not log_ids:
        return result
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT log_id, status FROM action_status WHERE log_id = ANY(%s)",
                (log_ids,),
            )
            for row in cur.fetchall():
                result[row["log_id"]] = row["status"]
    finally:
        if owns_conn:
            c.close()
    return result


def set_status(log_id: str, status: str, conn=None) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"잘못된 상태값입니다: {status} (허용값: {', '.join(VALID_STATUSES)})")
    sql = """
    INSERT INTO action_status (log_id, status, updated_at)
    VALUES (%s, %s, NOW())
    ON CONFLICT (log_id) DO UPDATE SET status = EXCLUDED.status, updated_at = NOW()
    """
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(sql, (log_id, status))
        c.commit()
    finally:
        if owns_conn:
            c.close()
