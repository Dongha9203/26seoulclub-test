import os
from typing import List, Optional, Dict, Tuple

import psycopg2
import psycopg2.extras

from models.document import Document


def _get_dsn() -> str:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise EnvironmentError("SUPABASE_DB_URL 환경변수가 설정되지 않았습니다.")
    return dsn


def get_connection():
    return psycopg2.connect(_get_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)


def _with_conn(conn):
    """conn이 주어지면 그대로 사용(닫지 않음), 없으면 새로 열고 호출자가 닫아야 함."""
    owns_conn = conn is None
    return (conn or get_connection()), owns_conn


_CREATE_DOCUMENTS_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    source_type     TEXT NOT NULL,
    source_origin   TEXT NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '미분류',
    notion_page_url TEXT,
    notion_block_id TEXT,
    last_updated    TEXT NOT NULL,
    is_editable     BOOLEAN NOT NULL DEFAULT TRUE,
    embedding       JSONB,
    embedding_model TEXT
)
"""

_CREATE_SYNC_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS sync_metadata (
    page_key                TEXT PRIMARY KEY,
    last_notion_edited_time TEXT,
    last_synced_at          TEXT
)
"""

_CREATE_QA_LOG_SQL = """
CREATE TABLE IF NOT EXISTS qa_log (
    log_id                      TEXT PRIMARY KEY,
    timestamp                   TIMESTAMPTZ NOT NULL,
    session_id                  TEXT NOT NULL,
    question                    TEXT NOT NULL,
    keywords                    JSONB,
    question_category           TEXT,
    blocked_by_filter           BOOLEAN NOT NULL,
    search_success              BOOLEAN,
    top_score                   DOUBLE PRECISION,
    failure_cause                TEXT,
    situation                   TEXT,
    response_attitude           TEXT,
    answer                      TEXT,
    sentiment_score             DOUBLE PRECISION,
    repeated_count               INTEGER NOT NULL DEFAULT 0,
    matched_doc_ids               JSONB,
    deep_link                    TEXT,
    escalated_to_operation_team  BOOLEAN NOT NULL,
    latency_ms                   INTEGER
)
"""


def initialize_db(conn=None) -> None:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(_CREATE_DOCUMENTS_SQL)
            cur.execute(_CREATE_SYNC_METADATA_SQL)
            cur.execute(_CREATE_QA_LOG_SQL)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_source_type ON documents(source_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_source_origin ON documents(source_origin)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_category ON documents(category)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_qa_log_session_timestamp "
                        "ON qa_log(session_id, timestamp DESC)")
        c.commit()
    finally:
        if owns_conn:
            c.close()


def upsert_document(doc: Document, conn=None) -> None:
    upsert_documents([doc], conn)


def upsert_documents(docs: List[Document], conn=None) -> int:
    if not docs:
        return 0
    sql = """
    INSERT INTO documents
        (doc_id, source_type, source_origin, title, content, category,
         notion_page_url, notion_block_id, last_updated, is_editable)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (doc_id) DO UPDATE SET
        source_type     = EXCLUDED.source_type,
        source_origin   = EXCLUDED.source_origin,
        title           = EXCLUDED.title,
        content         = EXCLUDED.content,
        category        = EXCLUDED.category,
        notion_page_url = EXCLUDED.notion_page_url,
        notion_block_id = EXCLUDED.notion_block_id,
        last_updated    = EXCLUDED.last_updated,
        is_editable     = EXCLUDED.is_editable
    """
    rows = [
        (d.doc_id, d.source_type, d.source_origin, d.title, d.content,
         d.category, d.notion_page_url, d.notion_block_id,
         d.last_updated, bool(d.is_editable))
        for d in docs
    ]
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.executemany(sql, rows)
        c.commit()
    finally:
        if owns_conn:
            c.close()
    return len(docs)


def delete_by_source_origin(source_origin: str, conn=None) -> int:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE source_origin = %s", (source_origin,))
            deleted = cur.rowcount
        c.commit()
        return deleted
    finally:
        if owns_conn:
            c.close()


def get_all(conn=None) -> List[Document]:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM documents ORDER BY last_updated DESC")
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [_row_to_doc(r) for r in rows]


def get_by_source_type(source_type: str, conn=None) -> List[Document]:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM documents WHERE source_type = %s", (source_type,))
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [_row_to_doc(r) for r in rows]


def get_by_source_origin(source_origin: str, conn=None) -> List[Document]:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM documents WHERE source_origin = %s", (source_origin,))
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [_row_to_doc(r) for r in rows]


def get_category_distribution(conn=None) -> Dict[str, int]:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT category, COUNT(*) AS cnt FROM documents GROUP BY category ORDER BY cnt DESC"
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return {r["category"]: r["cnt"] for r in rows}


def get_total_count(conn=None) -> int:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM documents")
            row = cur.fetchone()
    finally:
        if owns_conn:
            c.close()
    return row["cnt"]


def get_sync_metadata(page_key: str, conn=None) -> Optional[Dict]:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM sync_metadata WHERE page_key = %s", (page_key,))
            row = cur.fetchone()
    finally:
        if owns_conn:
            c.close()
    if row is None:
        return None
    return {
        "page_key": row["page_key"],
        "last_notion_edited_time": row["last_notion_edited_time"],
        "last_synced_at": row["last_synced_at"],
    }


def upsert_sync_metadata(page_key: str, last_edited: str, synced_at: str, conn=None) -> None:
    sql = """
    INSERT INTO sync_metadata (page_key, last_notion_edited_time, last_synced_at)
    VALUES (%s, %s, %s)
    ON CONFLICT (page_key) DO UPDATE SET
        last_notion_edited_time = EXCLUDED.last_notion_edited_time,
        last_synced_at          = EXCLUDED.last_synced_at
    """
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(sql, (page_key, last_edited, synced_at))
        c.commit()
    finally:
        if owns_conn:
            c.close()


def update_embedding(doc_id: str, embedding: List[float], model_name: str, conn=None) -> None:
    """문서 1건의 임베딩 벡터를 저장합니다 (JSONB)."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE documents SET embedding = %s, embedding_model = %s WHERE doc_id = %s",
                (psycopg2.extras.Json(embedding), model_name, doc_id),
            )
        c.commit()
    finally:
        if owns_conn:
            c.close()


def get_documents_missing_embedding(model_name: str, conn=None) -> List[Document]:
    """현재 모델 기준으로 임베딩이 없거나 다른 모델로 계산된 문서를 반환합니다 (백필 대상)."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM documents WHERE embedding IS NULL OR embedding_model IS NULL "
                "OR embedding_model != %s",
                (model_name,),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [_row_to_doc(r) for r in rows]


def get_all_with_embeddings(model_name: str, conn=None) -> List[Tuple[Document, Optional[List[float]]]]:
    """모든 문서를 (Document, embedding) 튜플로 반환합니다.
    embedding_model이 현재 모델과 다르면 embedding은 None으로 처리합니다 (모델 교체 시 무효화)."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM documents")
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()

    results = []
    for row in rows:
        doc = _row_to_doc(row)
        if row["embedding"] is not None and row["embedding_model"] == model_name:
            embedding = row["embedding"]
        else:
            embedding = None
        results.append((doc, embedding))
    return results


def insert_qa_log(entry: Dict, conn=None) -> None:
    sql = """
    INSERT INTO qa_log (
        log_id, timestamp, session_id, question, keywords, question_category,
        blocked_by_filter, search_success, top_score, failure_cause, situation,
        response_attitude, answer, sentiment_score, repeated_count, matched_doc_ids,
        deep_link, escalated_to_operation_team, latency_ms
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(sql, (
                entry["log_id"], entry["timestamp"], entry["session_id"], entry["question"],
                psycopg2.extras.Json(entry["keywords"]), entry["question_category"],
                entry["blocked_by_filter"], entry["search_success"], entry["top_score"],
                entry["failure_cause"], entry["situation"], entry["response_attitude"],
                entry["answer"], entry["sentiment_score"], entry["repeated_count"],
                psycopg2.extras.Json(entry["matched_doc_ids"]), entry["deep_link"],
                entry["escalated_to_operation_team"], entry["latency_ms"],
            ))
        c.commit()
    finally:
        if owns_conn:
            c.close()


def count_recent_requests(session_id: str, window_seconds: int, conn=None) -> int:
    """최근 window_seconds 동안 해당 session_id가 보낸 qa_log 건수 (API 남용 방지용)."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM qa_log "
                "WHERE session_id = %s AND timestamp >= NOW() - make_interval(secs => %s)",
                (session_id, window_seconds),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            c.close()
    return row["cnt"]


def get_recent_qa_logs(limit: int, conn=None) -> List[Dict]:
    """전체 세션 통틀어 timestamp 내림차순으로 최근 limit건을 반환합니다."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT session_id, keywords FROM qa_log ORDER BY timestamp DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [{"session_id": r["session_id"], "keywords": r["keywords"]} for r in rows]


def get_daily_qa_counts(limit: int = 30, offset: int = 0, conn=None) -> List[Dict]:
    """일별 질의/응답 건수를 최신 날짜부터 페이지 단위로 반환합니다 (모니터링 화면용)."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT DATE(timestamp) AS day, COUNT(*) AS cnt FROM qa_log "
                "GROUP BY DATE(timestamp) ORDER BY day DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [{"day": r["day"].isoformat(), "count": r["cnt"]} for r in rows]


def get_qa_logs_paginated(limit: int = 50, offset: int = 0, conn=None) -> List[Dict]:
    """질의-답변 연계조회: 최근 로그를 페이지 단위로 반환합니다."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM qa_log ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [dict(r) for r in rows]


def get_score_distribution(conn=None) -> Dict[str, int]:
    """top_score를 0.1 단위 구간으로 묶어 신뢰도 분포 차트 데이터를 만듭니다."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT WIDTH_BUCKET(top_score, 0, 1, 10) AS bucket, COUNT(*) AS cnt "
                "FROM qa_log WHERE top_score IS NOT NULL GROUP BY bucket ORDER BY bucket"
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    buckets = {f"{(b-1)/10:.1f}-{b/10:.1f}": 0 for b in range(1, 11)}
    for r in rows:
        b = max(1, min(10, r["bucket"]))
        key = f"{(b-1)/10:.1f}-{b/10:.1f}"
        buckets[key] = buckets.get(key, 0) + r["cnt"]
    return buckets


def delete_old_qa_logs(days: int = 365, conn=None) -> int:
    """보존기간(기본 1년)이 지난 qa_log 행을 삭제합니다 (일일 cron에서 호출).

    일별 건수/원인별 집계 리포트는 같은 qa_log를 그대로 집계하므로, 삭제 이후
    이 기간보다 오래된 날짜의 통계는 함께 사라집니다 — 의도된 동작입니다.
    action_status.log_id가 qa_log.log_id를 참조(FK)하므로, qa_log를 지우기
    전에 같은 log_id의 action_status 행을 먼저 지워야 FK 위반이 나지 않습니다."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "DELETE FROM action_status WHERE log_id IN ("
                "  SELECT log_id FROM qa_log WHERE timestamp < NOW() - make_interval(days => %s)"
                ")",
                (days,),
            )
            cur.execute(
                "DELETE FROM qa_log WHERE timestamp < NOW() - make_interval(days => %s)",
                (days,),
            )
            deleted = cur.rowcount
        c.commit()
        return deleted
    finally:
        if owns_conn:
            c.close()


def get_logs_by_failure_causes(causes: List[str], limit: int = 50, offset: int = 0,
                                conn=None) -> List[Dict]:
    """failure_cause가 주어진 목록에 속하는 로그를 조회합니다 (불완전답변/미해결답변 화면용).

    운영자가 노션/데이터를 수정해 처리 완료(action_status='완료')로 표시한 항목은
    목록에서 제외합니다. qa_log 행 자체는 지우지 않으므로 일별 건수/원인별 집계
    통계에는 계속 반영됩니다."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM qa_log WHERE failure_cause = ANY(%s) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM action_status"
                "  WHERE action_status.log_id = qa_log.log_id AND action_status.status = '완료'"
                ") "
                "ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                (causes, limit, offset),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return [dict(r) for r in rows]


def get_failure_cause_counts(conn=None) -> Dict[str, int]:
    """원인별 집계 리포트: failure_cause 4종 건수."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT failure_cause, COUNT(*) AS cnt FROM qa_log "
                "WHERE failure_cause IS NOT NULL GROUP BY failure_cause"
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            c.close()
    return {r["failure_cause"]: r["cnt"] for r in rows}


def _row_to_doc(row) -> Document:
    return Document(
        doc_id=row["doc_id"],
        source_type=row["source_type"],
        source_origin=row["source_origin"],
        title=row["title"],
        content=row["content"],
        category=row["category"],
        notion_page_url=row["notion_page_url"],
        notion_block_id=row["notion_block_id"],
        last_updated=row["last_updated"],
        is_editable=bool(row["is_editable"]),
    )
