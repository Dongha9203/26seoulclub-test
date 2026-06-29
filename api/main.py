"""
Vercel 단일 FastAPI entrypoint (pyproject.toml [tool.vercel] entrypoint = "api.main:app").

Vercel의 Python 빌더가 fastapi 의존성이 있으면 프로젝트 전체에서 단일 entrypoint를
요구하기 때문에(예전 파일별 독립 함수 모델 폐기), chat/admin/sync_notion/cron을
이 파일 하나로 합칩니다. 각 라우트의 외부 경로는 기존과 동일하게 유지합니다
(/api/chat, /api/admin/*, /api/sync_notion, /api/cron/sync_notion).
"""

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.chat import app as chat_app
from api.admin import app as admin_app
from api.sync_notion import _perform_sync
from api.cron.sync_notion import _perform_incremental_sync

logger = logging.getLogger(__name__)

_config_path = Path(__file__).parent.parent / "config.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = {}
    try:
        with open(_config_path, encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        logger.warning("startup: config.json 로드 실패", exc_info=True)

    model_name = config.get("embedding_model")
    if model_name:
        # 1. 코퍼스 캐시 예열 — DB 로드(~5초) + Kiwi 형태소분석기 초기화를
        #    첫 요청 전에 완료해 콜드 스타트 지연을 없앱니다.
        try:
            from hybrid_search import _load_corpus
            _load_corpus(model_name)
            logger.info("startup: 코퍼스 캐시 예열 완료 (model=%s)", model_name)
        except Exception:
            logger.warning("startup: 코퍼스 캐시 예열 실패 — 첫 요청에서 재시도됩니다", exc_info=True)

        # 2. Voyage AI HTTP 커넥션 예열 — 첫 호출에만 ~1.5초 발생하는
        #    TCP/TLS 수립 비용을 첫 요청 전에 지불합니다.
        try:
            from embedding_manager import get_embedding_provider
            get_embedding_provider(config).embed_query("예열")
            logger.info("startup: Voyage AI 커넥션 예열 완료")
        except Exception:
            logger.warning("startup: Voyage AI 커넥션 예열 실패 — 첫 요청에서 재시도됩니다", exc_info=True)

    yield


app = FastAPI(lifespan=lifespan)
app.include_router(chat_app.router)
app.include_router(admin_app.router, prefix="/api/admin")


@app.api_route("/api/sync_notion", methods=["GET", "POST"])
def sync_notion_route():
    return _perform_sync()


@app.get("/api/cron/sync_notion")
def cron_sync_notion_route():
    from storage.supabase_store import get_connection, initialize_db, insert_cron_run_log, update_cron_run_log
    run_id = str(uuid.uuid4())
    log_conn = get_connection()
    try:
        initialize_db(conn=log_conn)
        insert_cron_run_log(run_id, "sync_notion", conn=log_conn)
    except Exception:
        logger.exception("cron_run_log 시작 기록 실패 (무시하고 계속)")
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
        return result
    except Exception as e:
        logger.exception("cron sync_notion 처리 중 오류")
        try:
            log_conn = get_connection()
            update_cron_run_log(run_id, "error", error_message=str(e), conn=log_conn)
            log_conn.close()
        except Exception:
            logger.exception("cron_run_log 오류 기록 실패 (무시)")
        raise


# 운영 배포(Vercel)에서는 public/ 정적 파일을 플랫폼이 직접 서빙해 이 함수까지
# 오지 않지만, 로컬 uvicorn 실행에는 그 계층이 없어 위젯/대시보드 HTML이 전혀
# 응답되지 않습니다. 로컬 개발용으로만 마운트합니다.
# (반드시 다른 라우트 등록 뒤에 마운트해야 /api/* 라우트가 가려지지 않습니다).
_public_dir = Path(__file__).resolve().parent.parent / "public"
if _public_dir.exists():
    app.mount("/", StaticFiles(directory=str(_public_dir), html=True), name="static")
