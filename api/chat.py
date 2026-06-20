"""
공개 챗봇 API 엔드포인트: POST /api/chat

3단계: 노션 임베드 위젯(public/widget.html)이 호출하는 메인 엔드포인트.
chatbot_engine.ChatbotEngine.handle_question(question, session_id)을 그대로 사용합니다.

남용 방지(앱 단위 안전장치, 4단계 대시보드에서 운영자가 조정 가능):
  - max_question_length: 질문 글자수 제한
  - rate_limit_per_minute: 같은 session_id 기준 분당 요청 수 제한
    (qa_log의 session_id/timestamp 인덱스를 그대로 활용해 조회)

설정 구성: notion_pages/categories/embedding_model 등 정적 값은 config.json에서
한 번만 읽어 캐시하고, 담당자 연락처/신뢰도 threshold/톤 8요소/API 파라미터처럼
4단계 대시보드에서 수정 가능한 동적 값은 app_settings 테이블에서 매 요청마다
새로 읽습니다. ChatbotEngine 자체도 매 요청 새로 생성합니다(가벼운 객체 조립이라
비용은 무시할 수준이고, 그 대신 운영자가 대시보드에서 설정을 바꾸면 다음 요청부터
바로 반영됩니다 — 엔진을 캐싱하면 이 "즉시 반영" 요구사항을 만족할 수 없습니다).
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

_static_config: Optional[dict] = None


def _load_static_config() -> dict:
    global _static_config
    if _static_config is None:
        with open(_root / "config.json", "r", encoding="utf-8") as f:
            _static_config = json.load(f)
    return _static_config


def _build_config() -> dict:
    from storage.settings_store import get_settings

    static = _load_static_config()
    dynamic = get_settings()
    merged = dict(static)
    merged.update({
        "operation_team": dynamic["operation_team"],
        "search_weights": dynamic["search_weights"],
        "search_top_k": dynamic["search_top_k"],
        "repeat_threshold": dynamic["repeat_threshold"],
        "min_keywords_for_clarity": dynamic["min_keywords_for_clarity"],
        "max_question_length": dynamic["max_question_length"],
        "rate_limit_per_minute": dynamic["rate_limit_per_minute"],
        "tone_elements": dynamic["tone_elements"],
        "situation_keywords": dynamic["situation_keywords"],
        "forbidden_words": dynamic["forbidden_words"],
    })
    return merged


class ChatRequest(BaseModel):
    question: str
    session_id: str


class ChatResponse(BaseModel):
    answer: str
    deep_link: Optional[str] = None


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    from storage.supabase_store import count_recent_requests
    from chatbot_engine import ChatbotEngine

    config = _build_config()
    max_len = config.get("max_question_length", 500)
    rate_limit = config.get("rate_limit_per_minute", 10)

    if len(req.question) > max_len:
        return JSONResponse(
            status_code=400,
            content={"error": f"질문은 {max_len}자 이하로 입력해주세요."},
        )

    if count_recent_requests(req.session_id, 60) >= rate_limit:
        return JSONResponse(
            status_code=429,
            content={"error": "잠시 후 다시 시도해주세요."},
        )

    try:
        engine = ChatbotEngine(config)
        response = engine.handle_question(req.question, req.session_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        logger.exception("chat 처리 중 오류 발생")
        return JSONResponse(status_code=500, content={"error": "일시적인 오류가 발생했습니다."})

    return ChatResponse(answer=response.answer, deep_link=response.deep_link)
