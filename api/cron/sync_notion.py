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


def _perform_incremental_sync() -> dict:
    """변경된 노션 페이지만 재수집 (cron 전용 로직)."""
    import json as _json
    from collectors.notion_collector import sync_notion_pages_incremental
    from storage.supabase_store import initialize_db, delete_by_source_origin, upsert_documents
    from utils.validators import validate_notion_block_ids

    config_path = _root / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = _json.load(f)

    initialize_db()

    docs, summary = sync_notion_pages_incremental(config)

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

    inserted = upsert_documents(docs) if docs else 0
    validation = validate_notion_block_ids(docs) if docs else {}

    return {
        "status": "ok",
        "mode": "incremental",
        "total_collected": len(docs),
        "inserted": inserted,
        "pages": summary,
        "validation": validation,
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
