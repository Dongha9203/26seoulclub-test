"""
Vercel 단일 FastAPI entrypoint (pyproject.toml [tool.vercel] entrypoint = "api.main:app").

Vercel의 Python 빌더가 fastapi 의존성이 있으면 프로젝트 전체에서 단일 entrypoint를
요구하기 때문에(예전 파일별 독립 함수 모델 폐기), chat/admin/sync_notion/cron을
이 파일 하나로 합칩니다. 각 라우트의 외부 경로는 기존과 동일하게 유지합니다
(/api/chat, /api/admin/*, /api/sync_notion, /api/cron/sync_notion).
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.chat import app as chat_app
from api.admin import app as admin_app
from api.sync_notion import _perform_sync
from api.cron.sync_notion import _perform_incremental_sync

app = FastAPI()
app.include_router(chat_app.router)
app.include_router(admin_app.router, prefix="/api/admin")


@app.api_route("/api/sync_notion", methods=["GET", "POST"])
def sync_notion_route():
    return _perform_sync()


@app.get("/api/cron/sync_notion")
def cron_sync_notion_route():
    return _perform_incremental_sync()


# 운영 배포(Vercel)에서는 public/ 정적 파일을 플랫폼이 직접 서빙해 이 함수까지
# 오지 않지만, 로컬 uvicorn 실행에는 그 계층이 없어 위젯/대시보드 HTML이 전혀
# 응답되지 않습니다. 로컬 개발용으로 같은 정적 파일을 여기서도 서빙합니다
# (반드시 다른 라우트 등록 뒤에 마운트해야 /api/* 라우트가 가려지지 않습니다).
app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "public"), html=True),
          name="static")
