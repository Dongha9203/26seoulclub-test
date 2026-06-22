"""
3단계(공개 챗봇 API + 위젯) 테스트.

ChatbotEngine 자체는 2단계에서 이미 검증되었으므로 여기서는 mock으로 대체하고,
API 레이어의 책임(글자수 제한/남용 방지/에러 매핑)만 검증합니다.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# ══════════════════════════════════════════════════════════════
# storage/supabase_store.py — count_recent_requests
# ══════════════════════════════════════════════════════════════

class TestCountRecentRequests:
    def _make_log_entry(self, session_id):
        return {
            "log_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id, "question": "q", "keywords": [], "question_category": None,
            "blocked_by_filter": False, "search_success": None, "top_score": 0.0,
            "failure_cause": None, "situation": None, "response_attitude": None,
            "answer": "a", "sentiment_score": None, "repeated_count": 0,
            "matched_doc_ids": [], "deep_link": None, "escalated_to_operation_team": False,
            "latency_ms": 1,
        }

    def test_counts_requests_within_window(self, pg_conn):
        from storage.supabase_store import insert_qa_log, count_recent_requests
        session_id = "session-rate-test"
        for _ in range(3):
            insert_qa_log(self._make_log_entry(session_id), pg_conn)

        assert count_recent_requests(session_id, 60, pg_conn) == 3
        assert count_recent_requests("other-session", 60, pg_conn) == 0


# ══════════════════════════════════════════════════════════════
# api/chat.py
# ══════════════════════════════════════════════════════════════

_TONE_TEST_VALUES = {k: f"테스트-{k}" for k in
                     ["personality", "language_purity", "vip_consistency", "formality",
                      "channel", "emotional_labor", "persona", "factuality"]}


class TestChatApi:
    @pytest.fixture
    def client(self, monkeypatch, pg_conn):
        import api.chat as chat_module
        from storage.settings_store import (
            initialize_settings_db, seed_default_settings, get_settings, update_settings,
        )

        monkeypatch.setattr(chat_module, "_static_config", {
            "notion_pages": {}, "google_sheets": [], "embedding_model": "voyage-4",
            "llm_model": "claude-sonnet-4-6", "categories": [],
        })

        initialize_settings_db(pg_conn)
        try:
            original_settings = get_settings(pg_conn)
        except RuntimeError:
            original_settings = None

        test_settings = {
            "operation_team": {"name": "테스트팀", "address": "테스트주소", "phone": "000-0000-0000",
                                "email_list": ["test@test.com"], "operating_hours": "평일 9-18시"},
            "similarity_threshold": 0.55,
            "search_weights": {"vector": 0.6, "bm25": 0.4},
            "search_top_k": 5,
            "repeat_threshold": 2,
            "min_keywords_for_clarity": 1,
            "max_question_length": 500,
            "rate_limit_per_minute": 10,
            "tone_elements": _TONE_TEST_VALUES,
        }
        if original_settings is None:
            seed_default_settings(test_settings, pg_conn)
        else:
            update_settings(test_settings, pg_conn)

        self.fake_engine = MagicMock()
        monkeypatch.setattr("chatbot_engine.ChatbotEngine", MagicMock(return_value=self.fake_engine))
        # api/chat.py는 connection을 직접 열지 않고 항상 conn=None으로 호출하므로,
        # 같은 Supabase 프로젝트를 보는 pg_conn으로 사전에 적재한 데이터가 그대로 보입니다.
        self.pg_conn = pg_conn

        yield TestClient(chat_module.app)

        if original_settings is not None:
            update_settings(original_settings, pg_conn)

    def _make_log_entry(self, session_id):
        return {
            "log_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id, "question": "q", "keywords": [], "question_category": None,
            "blocked_by_filter": False, "search_success": None, "top_score": 0.0,
            "failure_cause": None, "situation": None, "response_attitude": None,
            "answer": "a", "sentiment_score": None, "repeated_count": 0,
            "matched_doc_ids": [], "deep_link": None, "escalated_to_operation_team": False,
            "latency_ms": 1,
        }

    def test_question_too_long_returns_400(self, client):
        res = client.post("/api/chat", json={"question": "가" * 501, "session_id": "session-1"})
        assert res.status_code == 400
        self.fake_engine.handle_question.assert_not_called()

    def test_rate_limit_exceeded_returns_429(self, client):
        from storage.supabase_store import insert_qa_log
        session_id = "session-rate-limited"
        for _ in range(10):
            insert_qa_log(self._make_log_entry(session_id), self.pg_conn)

        res = client.post("/api/chat", json={"question": "수당 지급 기준이 뭐예요?", "session_id": session_id})
        assert res.status_code == 429
        self.fake_engine.handle_question.assert_not_called()

    def test_successful_chat_returns_answer_and_deep_link(self, client):
        self.fake_engine.handle_question.return_value = MagicMock(
            answer="수당은 매월 말일 지급됩니다.", deep_link="https://notion.so/abc#123",
        )
        res = client.post("/api/chat", json={"question": "수당 언제 들어와요?", "session_id": "session-ok"})
        assert res.status_code == 200
        body = res.json()
        assert body["answer"] == "수당은 매월 말일 지급됩니다."
        assert body["deep_link"] == "https://notion.so/abc#123"

    def test_empty_question_returns_400(self, client):
        self.fake_engine.handle_question.side_effect = ValueError("질문이 비어있습니다.")
        res = client.post("/api/chat", json={"question": "", "session_id": "session-empty"})
        assert res.status_code == 400

    def test_internal_error_returns_500_without_leaking_detail(self, client):
        self.fake_engine.handle_question.side_effect = ConnectionError("anthropic api down")
        res = client.post("/api/chat", json={"question": "수당 지급 기준이 뭐예요?", "session_id": "session-err"})
        assert res.status_code == 500
        assert "anthropic api down" not in res.text
