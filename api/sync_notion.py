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
    from embedding_manager import get_embedding_provider, backfill_embeddings
    from storage.supabase_store import (
        get_connection, initialize_db, delete_by_source_origins, upsert_documents,
        get_documents_missing_embedding,
    )
    from utils.validators import validate_notion_block_ids

    config_path = _root / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = _json.load(f)

    # DB 작업 전체에서 커넥션 하나를 재사용합니다. 매 호출마다 새 커넥션을 열면
    # (문서 1건당 1회 포함) Vercel↔Supabase 간 연결 핸드셰이크가 수십 번
    # 누적되어 "지금 갱신"이 체감상 매우 느려지므로, 여기서 한 번만 열고 끝까지 씁니다.
    conn = get_connection()
    try:
        initialize_db(conn=conn)

        docs, summary = sync_notion_pages(config)

        # 최상위 페이지뿐 아니라, 그 안에서 재귀적으로 발견된 하위 페이지(child_page)도
        # 각자 고유한 source_origin을 가지므로, 이번에 실제로 수집된 모든 source_origin을
        # 기준으로 기존 Document를 지워야 매번 갱신할 때마다 하위 페이지가 중복 누적되지 않습니다.
        # source_origin별로 반복 삭제하지 않고 ANY(%s)로 한 번에 지워 라운드트립을 줄입니다.
        deleted = delete_by_source_origins(list({d.source_origin for d in docs}), conn=conn)
        logger.info("기존 Document 삭제: 총 %d건", deleted)

        inserted = upsert_documents(docs, conn=conn)
        validation = validate_notion_block_ids(docs)

        # summary(=pages)는 최상위 페이지 키 기준이라 그 안에 재귀로 묶인 하위 페이지
        # 문서 수가 전부 합산되어 보입니다. 운영자에게는 실제로 어떤 출처(하위 페이지
        # 포함)가 몇 건씩 갱신됐는지 보여줘야 하므로 source_origin별로 따로 집계합니다.
        sources: dict = {}
        for d in docs:
            sources[d.source_origin] = sources.get(d.source_origin, 0) + 1

        # 노션 동기화는 본문만 가져오고 임베딩은 별도 단계였습니다. 갱신 직후 바로
        # 검색에 반영되도록, 여기서 임베딩이 없는 노션 문서를 자동으로 백필합니다.
        model = config.get("embedding_model")
        embedded, embed_failed = 0, 0
        try:
            provider = get_embedding_provider(config)
            pending = [d for d in get_documents_missing_embedding(model, conn=conn) if d.source_type == "notion"]
            embedded, embed_failed = backfill_embeddings(pending, provider, model, conn=conn)
        except Exception:
            logger.exception("노션 문서 임베딩 백필 중 오류")
        if embedded or embed_failed:
            logger.info("노션 문서 임베딩 백필: %d건 성공, %d건 실패", embedded, embed_failed)
    finally:
        conn.close()

    return {
        "status": "ok",
        "total_collected": len(docs),
        "inserted": inserted,
        "pages": summary,
        "sources": sources,
        "validation": validation,
        "embedding": {"embedded": embedded, "failed": embed_failed},
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
