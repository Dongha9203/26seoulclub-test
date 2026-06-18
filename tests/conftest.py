import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

load_dotenv(_root / ".env")


# action_status는 qa_log를 FK로 참조하므로, 정리(삭제) 시 반드시 qa_log보다
# 먼저 삭제해야 ForeignKeyViolation을 피할 수 있습니다. dict 순서 = 삭제 순서.
_TABLE_PKS = {
    "documents": "doc_id", "sync_metadata": "page_key",
    "action_status": "log_id", "qa_log": "log_id",
    "admin.operators": "id", "app_settings": "id",
}


@pytest.fixture
def pg_conn():
    """실제 Supabase Postgres 프로젝트에 연결합니다.

    테스트가 끝나면 그 테스트 동안 새로 생긴 행만 삭제합니다 (테스트 시작 전에
    이미 있던 행은 다른 목적으로 보존 중인 데이터일 수 있으므로 건드리지 않습니다).
    """
    from storage.supabase_store import get_connection, initialize_db
    from storage.admin_store import initialize_admin_db
    from storage.settings_store import initialize_settings_db
    from storage.action_store import initialize_action_db

    conn = get_connection()
    initialize_db(conn)
    initialize_admin_db(conn)
    initialize_settings_db(conn)
    initialize_action_db(conn)

    pre_existing = {}
    with conn.cursor() as cur:
        for table, pk in _TABLE_PKS.items():
            cur.execute(f"SELECT {pk} FROM {table}")
            pre_existing[table] = {row[pk] for row in cur.fetchall()}

    yield conn

    with conn.cursor() as cur:
        for table, pk in _TABLE_PKS.items():
            cur.execute(f"SELECT {pk} FROM {table}")
            current_ids = {row[pk] for row in cur.fetchall()}
            new_ids = current_ids - pre_existing[table]
            if new_ids:
                cur.execute(
                    f"DELETE FROM {table} WHERE {pk} = ANY(%s)",
                    (list(new_ids),),
                )
    conn.commit()
    conn.close()
