"""
수동 트리거 엔드포인트: POST /api/sync_notion

운영자가 "지금 갱신" 버튼으로 노션 3페이지를 즉시 재수집합니다.
4단계 대시보드와 연동 예정.

Vercel Python 서버리스 함수 형식 (BaseHTTPRequestHandler).
"""

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Vercel 서버리스 환경에서 프로젝트 루트를 sys.path에 추가
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _perform_sync() -> dict:
    """노션 3페이지 전체 재수집 로직 (수동/cron 공통)."""
    import json as _json
    from collectors.notion_collector import sync_notion_pages
    from storage.supabase_store import initialize_db, delete_by_source_origin, upsert_documents
    from utils.validators import validate_notion_block_ids

    config_path = _root / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = _json.load(f)

    initialize_db()

    docs, summary = sync_notion_pages(config)

    page_name_map = {
        "main": "메인페이지",
        "integrated_system": "통합시스템",
        "faq": "FAQ",
    }

    for key, info in summary.items():
        if not info.get("skipped") and info.get("doc_count", 0) > 0:
            page_name = page_name_map.get(key, key)
            deleted = delete_by_source_origin(page_name)
            logger.info("기존 Document 삭제: %s → %d건", page_name, deleted)

    inserted = upsert_documents(docs)
    validation = validate_notion_block_ids(docs)

    return {
        "status": "ok",
        "total_collected": len(docs),
        "inserted": inserted,
        "pages": summary,
        "validation": validation,
    }


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._handle()

    def do_GET(self):
        self._handle()

    def _handle(self):
        try:
            result = _perform_sync()
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.exception("sync_notion 처리 중 오류 발생")
            body = json.dumps({"status": "error", "message": str(e)},
                               ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        logger.info(format, *args)
