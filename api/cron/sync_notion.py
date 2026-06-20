"""
Vercel Cron Job 엔드포인트: GET /api/cron/sync_notion
vercel.json 스케줄: "0 19 * * *" (매일 04:00 KST = 19:00 UTC 전날)

Hobby 플랜 제약: 하루 1회 이상 실행 불가.
  - Hobby 플랜: 최소 간격 1일 (daily)
  - Pro 플랜 이상: 분 단위 가능 (예: "*/5 * * * *")

이 엔드포인트는 호출될 때마다:
  1. 각 노션 페이지의 last_edited_time을 조회
  2. 이전 동기화 시각과 비교해 변경된 페이지만 재수집
  3. 즉시 종료 (영구 루프 아님)

두 엔드포인트(수동/cron) 모두 같은 내부 함수를 호출해 코드 중복을 방지합니다.

Vercel Python 서버리스 함수 형식 (BaseHTTPRequestHandler).
"""

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


_QA_LOG_RETENTION_DAYS = 365


def _perform_incremental_sync() -> dict:
    """변경된 노션 페이지만 재수집 (cron 전용 로직)."""
    import json as _json
    from collectors.notion_collector import sync_notion_pages_incremental
    from embedding_manager import get_embedding_provider, backfill_embeddings
    from storage.supabase_store import (
        initialize_db, delete_by_source_origin, upsert_documents, delete_old_qa_logs,
        get_documents_missing_embedding,
    )
    from utils.validators import validate_notion_block_ids

    config_path = _root / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = _json.load(f)

    initialize_db()

    docs, summary = sync_notion_pages_incremental(config)

    # 재귀로 발견되는 하위 페이지도 각자 고유한 source_origin을 가지므로, 이번에
    # 실제로 재수집된 모든 source_origin을 기준으로 기존 Document를 지웁니다.
    for source_origin in {d.source_origin for d in docs}:
        deleted = delete_by_source_origin(source_origin)
        logger.info("기존 Document 삭제: %s → %d건", source_origin, deleted)

    inserted = upsert_documents(docs) if docs else 0
    validation = validate_notion_block_ids(docs) if docs else {}

    sources: dict = {}
    for d in docs:
        sources[d.source_origin] = sources.get(d.source_origin, 0) + 1

    # 노션 동기화는 본문만 가져오고 임베딩은 별도 단계였습니다. 변경분이 바로
    # 검색에 반영되도록, 여기서 임베딩이 없는 노션 문서를 자동으로 백필합니다.
    model = config.get("embedding_model")
    embedded, embed_failed = 0, 0
    try:
        provider = get_embedding_provider(config)
        pending = [d for d in get_documents_missing_embedding(model) if d.source_type == "notion"]
        embedded, embed_failed = backfill_embeddings(pending, provider, model)
    except Exception:
        logger.exception("노션 문서 임베딩 백필 중 오류")
    if embedded or embed_failed:
        logger.info("노션 문서 임베딩 백필: %d건 성공, %d건 실패", embedded, embed_failed)

    qa_log_purged = delete_old_qa_logs(_QA_LOG_RETENTION_DAYS)
    logger.info("보존기간(%d일) 초과 qa_log 삭제: %d건", _QA_LOG_RETENTION_DAYS, qa_log_purged)

    return {
        "status": "ok",
        "mode": "incremental",
        "total_collected": len(docs),
        "inserted": inserted,
        "pages": summary,
        "sources": sources,
        "validation": validation,
        "qa_log_purged": qa_log_purged,
        "embedding": {"embedded": embedded, "failed": embed_failed},
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def _handle(self):
        try:
            result = _perform_incremental_sync()
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.exception("cron sync_notion 처리 중 오류 발생")
            body = json.dumps({"status": "error", "message": str(e)},
                               ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        logger.info(format, *args)
