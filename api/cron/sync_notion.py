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
import uuid
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
    from api.sync_notion import _sync_calendars, _sync_airtable
    from collectors.notion_collector import sync_notion_pages_incremental
    from embedding_manager import get_embedding_provider, backfill_embeddings
    from storage.supabase_store import (
        get_connection, initialize_db, get_by_source_origins, sync_documents_incrementally,
        delete_old_qa_logs, get_documents_missing_embedding,
    )
    from utils.validators import validate_notion_block_ids

    config_path = _root / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = _json.load(f)

    # DB 작업 전체에서 커넥션 하나를 재사용합니다 (문서 1건당 새 커넥션을 여는
    # 누적 비용을 피하기 위함 — api/sync_notion.py의 수동 갱신과 동일한 이유).
    conn = get_connection()
    try:
        initialize_db(conn=conn)

        docs, summary = sync_notion_pages_incremental(config, conn=conn)

        # sync_notion_pages_incremental은 메타데이터상 변경이 감지된 페이지만 재수집하므로
        # docs는 그 페이지들의 source_origin만 포함합니다. 변경되지 않은 페이지는 비교 대상에서
        # 빼야(=조회조차 하지 않아야) 그 문서들의 임베딩이 그대로 보존됩니다. 재수집된
        # source_origin 범위 안에서만 기존 Document와 diff해 실제로 내용이 바뀐 chunk만
        # 갱신하고, 그대로인 chunk는 임베딩을 보존합니다 (수동 "지금 갱신"과 동일한 방식).
        changed_source_origins = list({d.source_origin for d in docs})
        existing_docs = get_by_source_origins(changed_source_origins, conn=conn) if changed_source_origins else []
        changed_docs, deleted = sync_documents_incrementally(existing_docs, docs, conn=conn)
        logger.info("노션 incremental 동기화: 변경 %d건, 삭제 %d건, 변경없음(보존) %d건",
                    len(changed_docs), deleted, len(docs) - len(changed_docs))

        inserted = len(changed_docs)
        validation = validate_notion_block_ids(docs) if docs else {}

        sources: dict = {}
        for d in changed_docs:
            sources[d.source_origin] = sources.get(d.source_origin, 0) + 1

        # 구글 캘린더도 매일 새벽 자동 갱신에 묶습니다 — 수동 "지금 갱신"
        # (api/sync_notion.py)과 동일한 _sync_calendars를 그대로 재사용합니다.
        calendar_docs, calendar_sources = _sync_calendars(config, conn)
        docs.extend(calendar_docs)
        inserted += len(calendar_docs)
        sources.update(calendar_sources)

        # Airtable도 매일 자동 갱신에 묶습니다.
        airtable_docs, airtable_sources = _sync_airtable(config, conn)
        docs.extend(airtable_docs)
        inserted += len(airtable_docs)
        sources.update(airtable_sources)

        # 변경분이 바로 검색에 반영되도록, 임베딩이 없는 문서를 자동으로 백필합니다.
        model = config.get("embedding_model")
        embedded, embed_failed = 0, 0
        try:
            provider = get_embedding_provider(config)
            pending = [
                d for d in get_documents_missing_embedding(model, conn=conn)
                if d.source_type in ("notion", "google_calendar", "airtable")
            ]
            embedded, embed_failed = backfill_embeddings(pending, provider, model, conn=conn)
        except Exception:
            logger.exception("노션/캘린더 문서 임베딩 백필 중 오류")
        if embedded or embed_failed:
            logger.info("노션/캘린더 문서 임베딩 백필: %d건 성공, %d건 실패", embedded, embed_failed)

        qa_log_purged = delete_old_qa_logs(_QA_LOG_RETENTION_DAYS, conn=conn)
        logger.info("보존기간(%d일) 초과 qa_log 삭제: %d건", _QA_LOG_RETENTION_DAYS, qa_log_purged)
    finally:
        conn.close()

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
        from storage.supabase_store import (
            get_connection, initialize_db,
            insert_cron_run_log, update_cron_run_log,
        )
        # initialize_db를 먼저 호출해 cron_run_log 테이블이 없는 환경에서도 첫 실행부터 기록되도록 합니다.
        log_conn = get_connection()
        try:
            initialize_db(conn=log_conn)
            run_id = str(uuid.uuid4())
            insert_cron_run_log(run_id, "sync_notion", conn=log_conn)
        except Exception:
            logger.exception("cron_run_log 시작 기록 실패 (무시하고 계속)")
            run_id = str(uuid.uuid4())
        finally:
            log_conn.close()

        try:
            result = _perform_incremental_sync()
            try:
                log_conn = get_connection()
                update_cron_run_log(run_id, "ok", result=result, conn=log_conn)
                log_conn.close()
            except Exception:
                logger.exception("cron_run_log 완료 기록 실패 (무시)")
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.exception("cron sync_notion 처리 중 오류 발생")
            try:
                log_conn = get_connection()
                update_cron_run_log(run_id, "error", error_message=str(e), conn=log_conn)
                log_conn.close()
            except Exception:
                logger.exception("cron_run_log 오류 기록 실패 (무시)")
            body = json.dumps({"status": "error", "message": str(e)},
                               ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        logger.info(format, *args)
