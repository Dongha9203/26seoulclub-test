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


def _sync_calendars(config: dict, conn) -> tuple:
    """config.json의 google_calendars 목록을 incremental하게 동기화합니다.

    캘린더 일정은 calendar_collector가 (캘린더+이벤트 UID+발생 시작시각) 기준으로
    고정된 doc_id를 만들어주므로, storage.sync_documents_incrementally가 내용이
    안 바뀐 일정은 그대로 두어 임베딩을 보존하고, 실제로 바뀐/새로 생긴/사라진
    일정만 갱신합니다. 매번 80개를 통째로 지우고 새로 넣으면 안 바뀐 일정까지
    매번 재임베딩이 필요해져 Voyage rate limit에 매일 다시 걸리는 문제가 있었습니다.

    캘린더 하나가 비공개로 바뀌었거나 일시적으로 응답하지 않아도 노션 동기화
    전체를 막으면 안 되므로, 캘린더별 실패는 로그만 남기고 다음 캘린더로
    계속 진행합니다. 반환값(docs, sources)은 "이번에 실제로 갱신된" 것만
    담습니다 — 호출부의 inserted/sources 집계가 "변경 건수"를 의미하기 때문입니다.
    """
    from collectors.calendar_collector import calendar_source_origin, collect_google_calendar
    from storage.supabase_store import get_by_source_origin, sync_documents_incrementally

    changed_docs = []
    sources: dict = {}

    for url in config.get("google_calendars", []):
        try:
            fresh_docs = collect_google_calendar(url)
        except ValueError:
            logger.exception("구글 캘린더 수집 실패 (건너뜀): %s", url)
            continue

        # fresh_docs가 빈 경우(일정이 전부 취소/만료)에도 source_origin은 URL에서
        # 바로 계산 가능해야 기존 행을 정리할 수 있습니다(아래에서 전부 removed 처리).
        source_origin = calendar_source_origin(url)
        existing_docs = get_by_source_origin(source_origin, conn=conn)
        changed, deleted = sync_documents_incrementally(existing_docs, fresh_docs, conn=conn)

        if changed or deleted:
            logger.info(
                "캘린더 incremental 동기화: %s — 변경 %d건, 삭제 %d건, 변경없음(보존) %d건",
                source_origin, len(changed), deleted, len(fresh_docs) - len(changed),
            )

        changed_docs.extend(changed)
        sources[source_origin] = len(changed)

    return changed_docs, sources


def _perform_sync() -> dict:
    """노션 3페이지 + 구글 캘린더 전체 재수집 로직 (수동/cron 공통)."""
    import json as _json
    from collectors.notion_collector import sync_notion_pages
    from embedding_manager import get_embedding_provider, backfill_embeddings
    from storage.supabase_store import (
        get_connection, initialize_db, get_by_source_type, sync_documents_incrementally,
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
        validation = validate_notion_block_ids(docs)

        # notion_collector가 (notion_block_id+part_index) 기준으로 고정된 doc_id를
        # 만들어주므로, 내용이 안 바뀐 페이지/섹션은 DB 행을 그대로 두어 임베딩을
        # 보존합니다. 예전엔 매번 전체 삭제 후 재삽입이라 "지금 갱신"을 누를 때마다
        # 71건 전부가 재임베딩 대상이 되어 Voyage rate limit에 매번 걸렸습니다.
        existing_notion_docs = get_by_source_type("notion", conn=conn)
        changed_docs, deleted = sync_documents_incrementally(existing_notion_docs, docs, conn=conn)
        logger.info(
            "노션 incremental 동기화: 변경 %d건, 삭제 %d건, 변경없음(보존) %d건",
            len(changed_docs), deleted, len(docs) - len(changed_docs),
        )
        inserted = len(changed_docs)

        # summary(=pages)는 최상위 페이지 키 기준이라 그 안에 재귀로 묶인 하위 페이지
        # 문서 수가 전부 합산되어 보입니다. 운영자에게는 실제로 어떤 출처(하위 페이지
        # 포함)가 몇 건씩 갱신됐는지 보여줘야 하므로 source_origin별로 따로 집계합니다.
        # (실제로 바뀐 문서만 — 전체 건수가 아니라 "변경 건수"를 보여주는 게 맞습니다.)
        sources: dict = {}
        for d in changed_docs:
            sources[d.source_origin] = sources.get(d.source_origin, 0) + 1

        # 구글 캘린더(노션 안에 embed된 캘린더의 실제 일정)도 같은 "지금 갱신"/cron에
        # 묶어서 동기화합니다 — 운영자가 따로 버튼을 누를 필요가 없도록.
        calendar_docs, calendar_sources = _sync_calendars(config, conn)
        docs.extend(calendar_docs)
        inserted += len(calendar_docs)
        sources.update(calendar_sources)

        # 노션 동기화는 본문만 가져오고 임베딩은 별도 단계였습니다. 갱신 직후 바로
        # 검색에 반영되도록, 여기서 임베딩이 없는 노션/캘린더 문서를 자동으로 백필합니다.
        model = config.get("embedding_model")
        embedded, embed_failed = 0, 0
        try:
            provider = get_embedding_provider(config)
            pending = [
                d for d in get_documents_missing_embedding(model, conn=conn)
                if d.source_type in ("notion", "google_calendar")
            ]
            embedded, embed_failed = backfill_embeddings(pending, provider, model, conn=conn)
        except Exception:
            logger.exception("노션/캘린더 문서 임베딩 백필 중 오류")
        if embedded or embed_failed:
            logger.info("노션/캘린더 문서 임베딩 백필: %d건 성공, %d건 실패", embedded, embed_failed)
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
