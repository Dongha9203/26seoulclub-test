import json
import sqlite3
import os
from pathlib import Path
from typing import List, Optional, Dict, Tuple

from models.document import Document

_DEFAULT_DB = Path(__file__).parent.parent / "data" / "knowledge_base.db"


def _get_db_path() -> Path:
    env_path = os.environ.get("DB_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or _get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


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
    is_editable     INTEGER NOT NULL DEFAULT 1
)
"""

_CREATE_SYNC_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS sync_metadata (
    page_key                TEXT PRIMARY KEY,
    last_notion_edited_time TEXT,
    last_synced_at          TEXT
)
"""


def _ensure_embedding_columns(conn: sqlite3.Connection) -> None:
    """2단계: documents 테이블에 embedding/embedding_model 컬럼이 없으면 추가합니다."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    if "embedding" not in existing:
        conn.execute("ALTER TABLE documents ADD COLUMN embedding TEXT")
    if "embedding_model" not in existing:
        conn.execute("ALTER TABLE documents ADD COLUMN embedding_model TEXT")


def initialize_db(db_path: Optional[Path] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(_CREATE_DOCUMENTS_SQL)
        conn.execute(_CREATE_SYNC_METADATA_SQL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source_type ON documents(source_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source_origin ON documents(source_origin)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON documents(category)")
        _ensure_embedding_columns(conn)
        conn.commit()


def upsert_document(doc: Document, db_path: Optional[Path] = None) -> None:
    sql = """
    INSERT INTO documents
        (doc_id, source_type, source_origin, title, content, category,
         notion_page_url, notion_block_id, last_updated, is_editable)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(doc_id) DO UPDATE SET
        source_type     = excluded.source_type,
        source_origin   = excluded.source_origin,
        title           = excluded.title,
        content         = excluded.content,
        category        = excluded.category,
        notion_page_url = excluded.notion_page_url,
        notion_block_id = excluded.notion_block_id,
        last_updated    = excluded.last_updated,
        is_editable     = excluded.is_editable
    """
    with get_connection(db_path) as conn:
        conn.execute(sql, (
            doc.doc_id, doc.source_type, doc.source_origin, doc.title, doc.content,
            doc.category, doc.notion_page_url, doc.notion_block_id,
            doc.last_updated, 1 if doc.is_editable else 0,
        ))
        conn.commit()


def upsert_documents(docs: List[Document], db_path: Optional[Path] = None) -> int:
    if not docs:
        return 0
    sql = """
    INSERT INTO documents
        (doc_id, source_type, source_origin, title, content, category,
         notion_page_url, notion_block_id, last_updated, is_editable)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(doc_id) DO UPDATE SET
        source_type     = excluded.source_type,
        source_origin   = excluded.source_origin,
        title           = excluded.title,
        content         = excluded.content,
        category        = excluded.category,
        notion_page_url = excluded.notion_page_url,
        notion_block_id = excluded.notion_block_id,
        last_updated    = excluded.last_updated,
        is_editable     = excluded.is_editable
    """
    rows = [
        (d.doc_id, d.source_type, d.source_origin, d.title, d.content,
         d.category, d.notion_page_url, d.notion_block_id,
         d.last_updated, 1 if d.is_editable else 0)
        for d in docs
    ]
    with get_connection(db_path) as conn:
        conn.executemany(sql, rows)
        conn.commit()
    return len(docs)


def delete_by_source_origin(source_origin: str, db_path: Optional[Path] = None) -> int:
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM documents WHERE source_origin = ?", (source_origin,)
        )
        conn.commit()
        return cursor.rowcount


def get_all(db_path: Optional[Path] = None) -> List[Document]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM documents ORDER BY last_updated DESC"
        ).fetchall()
    return [_row_to_doc(r) for r in rows]


def get_by_source_type(source_type: str, db_path: Optional[Path] = None) -> List[Document]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE source_type = ?", (source_type,)
        ).fetchall()
    return [_row_to_doc(r) for r in rows]


def get_by_source_origin(source_origin: str, db_path: Optional[Path] = None) -> List[Document]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE source_origin = ?", (source_origin,)
        ).fetchall()
    return [_row_to_doc(r) for r in rows]


def get_category_distribution(db_path: Optional[Path] = None) -> Dict[str, int]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS cnt FROM documents GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
    return {r["category"]: r["cnt"] for r in rows}


def get_total_count(db_path: Optional[Path] = None) -> int:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM documents").fetchone()
    return row["cnt"]


def get_sync_metadata(page_key: str, db_path: Optional[Path] = None) -> Optional[Dict]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sync_metadata WHERE page_key = ?", (page_key,)
        ).fetchone()
    if row is None:
        return None
    return {
        "page_key": row["page_key"],
        "last_notion_edited_time": row["last_notion_edited_time"],
        "last_synced_at": row["last_synced_at"],
    }


def upsert_sync_metadata(page_key: str, last_edited: str, synced_at: str,
                          db_path: Optional[Path] = None) -> None:
    sql = """
    INSERT INTO sync_metadata (page_key, last_notion_edited_time, last_synced_at)
    VALUES (?, ?, ?)
    ON CONFLICT(page_key) DO UPDATE SET
        last_notion_edited_time = excluded.last_notion_edited_time,
        last_synced_at          = excluded.last_synced_at
    """
    with get_connection(db_path) as conn:
        conn.execute(sql, (page_key, last_edited, synced_at))
        conn.commit()


def update_embedding(doc_id: str, embedding: List[float], model_name: str,
                      db_path: Optional[Path] = None) -> None:
    """문서 1건의 임베딩 벡터를 저장합니다 (JSON 인코딩)."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE documents SET embedding = ?, embedding_model = ? WHERE doc_id = ?",
            (json.dumps(embedding), model_name, doc_id),
        )
        conn.commit()


def get_documents_missing_embedding(model_name: str, db_path: Optional[Path] = None) -> List[Document]:
    """현재 모델 기준으로 임베딩이 없거나 다른 모델로 계산된 문서를 반환합니다 (백필 대상)."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE embedding IS NULL OR embedding_model IS NULL "
            "OR embedding_model != ?",
            (model_name,),
        ).fetchall()
    return [_row_to_doc(r) for r in rows]


def get_all_with_embeddings(model_name: str, db_path: Optional[Path] = None) -> List[Tuple[Document, Optional[List[float]]]]:
    """모든 문서를 (Document, embedding) 튜플로 반환합니다.
    embedding_model이 현재 모델과 다르면 embedding은 None으로 처리합니다 (모델 교체 시 무효화)."""
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM documents").fetchall()

    results = []
    for row in rows:
        doc = _row_to_doc(row)
        emb_json = row["embedding"]
        emb_model = row["embedding_model"]
        if emb_json and emb_model == model_name:
            embedding = json.loads(emb_json)
        else:
            embedding = None
        results.append((doc, embedding))
    return results


def _row_to_doc(row: sqlite3.Row) -> Document:
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
