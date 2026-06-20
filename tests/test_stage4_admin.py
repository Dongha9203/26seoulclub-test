"""
4단계(운영 대시보드 + 인증) 테스트.
"""

import io
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


_TONE_TEST_VALUES = {k: f"테스트-{k}" for k in
                     ["personality", "language_purity", "vip_consistency", "formality",
                      "channel", "emotional_labor", "persona", "factuality"]}


def _make_log_entry(session_id="s1", failure_cause=None, top_score=0.5, timestamp=None):
    return {
        "log_id": str(uuid.uuid4()), "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        "session_id": session_id, "question": "테스트 질문", "keywords": ["테스트"],
        "question_category": "미분류", "blocked_by_filter": False, "search_success": failure_cause is None,
        "top_score": top_score, "failure_cause": failure_cause, "situation": None,
        "response_attitude": None, "answer": "테스트 답변", "sentiment_score": 0.0,
        "repeated_count": 0, "matched_doc_ids": [], "deep_link": None,
        "escalated_to_operation_team": failure_cause is not None, "latency_ms": 10,
    }


# ══════════════════════════════════════════════════════════════
# auth.py
# ══════════════════════════════════════════════════════════════

class TestAuth:
    def test_hash_and_verify_password_roundtrip(self):
        from auth import hash_password, verify_password
        h = hash_password("correct-password-123")
        assert verify_password("correct-password-123", h) is True

    def test_verify_password_wrong_password_fails(self):
        from auth import hash_password, verify_password
        h = hash_password("correct-password-123")
        assert verify_password("wrong-password", h) is False

    def test_create_and_decode_access_token_roundtrip(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        from auth import create_access_token, decode_access_token
        token = create_access_token("ops@test.com")
        assert decode_access_token(token) == "ops@test.com"

    def test_decode_access_token_expired_raises(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        from auth import decode_access_token, JWT_ALGORITHM
        now = datetime.now(timezone.utc)
        expired = jwt.encode(
            {"sub": "ops@test.com", "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
            "test-secret", algorithm=JWT_ALGORITHM,
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_access_token(expired)

    def test_decode_access_token_wrong_secret_raises(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        from auth import JWT_ALGORITHM, decode_access_token
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {"sub": "x", "iat": now, "exp": now + timedelta(hours=1)},
            "different-secret", algorithm=JWT_ALGORITHM,
        )
        with pytest.raises(jwt.InvalidTokenError):
            decode_access_token(token)

    def test_get_current_operator_missing_header_raises_401(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        from fastapi import HTTPException
        from auth import get_current_operator
        with pytest.raises(HTTPException) as exc:
            get_current_operator(None)
        assert exc.value.status_code == 401

    def test_get_current_operator_malformed_header_raises_401(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        from fastapi import HTTPException
        from auth import get_current_operator
        with pytest.raises(HTTPException) as exc:
            get_current_operator("NotBearer abc")
        assert exc.value.status_code == 401

    def test_get_current_operator_valid_token_returns_email(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        from auth import create_access_token, get_current_operator
        token = create_access_token("ops@test.com")
        assert get_current_operator("Bearer " + token) == "ops@test.com"


# ══════════════════════════════════════════════════════════════
# storage/admin_store.py
# ══════════════════════════════════════════════════════════════

class TestAdminStore:
    def test_create_and_get_operator(self, pg_conn):
        from storage.admin_store import create_operator, get_operator_by_email
        email = f"op-{uuid.uuid4()}@test.com"
        create_operator(email, "hashed-value", pg_conn)
        row = get_operator_by_email(email, pg_conn)
        assert row["email"] == email
        assert row["password_hash"] == "hashed-value"

    def test_get_operator_by_email_unknown_returns_none(self, pg_conn):
        from storage.admin_store import get_operator_by_email
        assert get_operator_by_email("nobody@test.com", pg_conn) is None

    def test_update_password_changes_hash(self, pg_conn):
        from storage.admin_store import create_operator, update_password, get_operator_by_email
        email = f"op-{uuid.uuid4()}@test.com"
        create_operator(email, "old-hash", pg_conn)
        updated = update_password(email, "new-hash", pg_conn)
        assert updated is True
        assert get_operator_by_email(email, pg_conn)["password_hash"] == "new-hash"

    def test_update_password_unknown_email_returns_false(self, pg_conn):
        from storage.admin_store import update_password
        assert update_password("nobody@test.com", "x", pg_conn) is False


# ══════════════════════════════════════════════════════════════
# storage/settings_store.py
# ══════════════════════════════════════════════════════════════

class TestSettingsStore:
    def _defaults(self):
        return {
            "operation_team": {"name": "팀", "address": "주소", "phone": "000", "email_list": ["a@b.com"], "operating_hours": "9-18"},
            "similarity_threshold": 0.55, "search_weights": {"vector": 0.6, "bm25": 0.4},
            "search_top_k": 5, "repeat_threshold": 2, "min_keywords_for_clarity": 1,
            "max_question_length": 500, "rate_limit_per_minute": 10,
            "tone_elements": _TONE_TEST_VALUES,
        }

    # app_settings는 실제 운영 DB에 항상 정확히 1행만 존재하는 싱글톤 테이블이라,
    # "행이 없는 상태"는 운영 데이터를 지우지 않고는 실제 DB로 재현할 수 없습니다.
    # 그래서 그 분기만 cursor를 mock하여 검증하고, 나머지는 실제 DB에 대해
    # 원래 값을 저장해뒀다가 테스트 후 반드시 복원합니다(운영 설정 오염 방지).

    def test_get_settings_raises_when_no_row(self):
        from storage.settings_store import get_settings
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        with pytest.raises(RuntimeError):
            get_settings(mock_conn)

    def test_seed_default_settings_inserts_when_no_row(self):
        from storage.settings_store import seed_default_settings
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"cnt": 0}
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        seed_default_settings(self._defaults(), mock_conn)
        insert_call = mock_cursor.execute.call_args_list[-1]
        assert "INSERT INTO app_settings" in insert_call[0][0]

    def test_seed_default_settings_noop_when_row_exists(self, pg_conn):
        from storage.settings_store import seed_default_settings, get_settings
        before = get_settings(pg_conn)
        different = self._defaults()
        different["similarity_threshold"] = 0.01
        seed_default_settings(different, pg_conn)  # 이미 행이 있으므로 무시되어야 함
        assert get_settings(pg_conn)["similarity_threshold"] == before["similarity_threshold"]

    def test_update_settings_partial_simple_key(self, pg_conn):
        from storage.settings_store import get_settings, update_settings
        original = get_settings(pg_conn)
        try:
            updated = update_settings({"similarity_threshold": 0.7}, pg_conn)
            assert updated["similarity_threshold"] == 0.7
            assert updated["search_top_k"] == original["search_top_k"]  # 건드리지 않은 값 유지
        finally:
            update_settings(original, pg_conn)

    def test_update_settings_tone_partial(self, pg_conn):
        from storage.settings_store import get_settings, update_settings
        original = get_settings(pg_conn)
        try:
            updated = update_settings({"tone_elements": {"personality": "새 성격"}}, pg_conn)
            assert updated["tone_elements"]["personality"] == "새 성격"
            assert updated["tone_elements"]["formality"] == original["tone_elements"]["formality"]
        finally:
            update_settings(original, pg_conn)

    def test_update_settings_situation_keywords_partial(self, pg_conn):
        from storage.settings_store import get_settings, update_settings
        original = get_settings(pg_conn)
        try:
            new_keywords = {
                "policy_violation": ["대리 출석", "대신 출석"],
                "escalation_request": ["상담원"],
                "gratitude": ["감사합니다"],
                "simple_rejection": ["안 할래요"],
            }
            updated = update_settings({"situation_keywords": new_keywords}, pg_conn)
            assert updated["situation_keywords"] == new_keywords
            assert updated["forbidden_words"] == original["forbidden_words"]  # 건드리지 않은 값 유지
        finally:
            update_settings(original, pg_conn)

    def test_update_settings_forbidden_words_partial(self, pg_conn):
        from storage.settings_store import get_settings, update_settings
        original = get_settings(pg_conn)
        try:
            new_words = {"profanity": ["욕설1"], "hate_speech": [], "threats": ["협박1"]}
            updated = update_settings({"forbidden_words": new_words}, pg_conn)
            assert updated["forbidden_words"] == new_words
        finally:
            update_settings(original, pg_conn)


# ══════════════════════════════════════════════════════════════
# storage/action_store.py
# ══════════════════════════════════════════════════════════════

class TestActionStore:
    def test_get_status_defaults_to_pending(self, pg_conn):
        from storage.action_store import get_status
        from storage.supabase_store import insert_qa_log
        entry = _make_log_entry()
        insert_qa_log(entry, pg_conn)
        assert get_status(entry["log_id"], pg_conn) == "대기"

    def test_set_status_then_get(self, pg_conn):
        from storage.action_store import set_status, get_status
        from storage.supabase_store import insert_qa_log
        entry = _make_log_entry()
        insert_qa_log(entry, pg_conn)
        set_status(entry["log_id"], "처리중", pg_conn)
        assert get_status(entry["log_id"], pg_conn) == "처리중"

    def test_set_status_invalid_value_raises(self, pg_conn):
        from storage.action_store import set_status
        with pytest.raises(ValueError):
            set_status("nonexistent", "보류", pg_conn)

    def test_get_statuses_batch(self, pg_conn):
        from storage.action_store import set_status, get_statuses
        from storage.supabase_store import insert_qa_log
        e1, e2 = _make_log_entry(), _make_log_entry()
        insert_qa_log(e1, pg_conn)
        insert_qa_log(e2, pg_conn)
        set_status(e1["log_id"], "완료", pg_conn)
        result = get_statuses([e1["log_id"], e2["log_id"]], pg_conn)
        assert result[e1["log_id"]] == "완료"
        assert result[e2["log_id"]] == "대기"

    def test_get_statuses_empty_list(self, pg_conn):
        from storage.action_store import get_statuses
        assert get_statuses([], pg_conn) == {}


# ══════════════════════════════════════════════════════════════
# storage/supabase_store.py — 4단계 추가 집계 함수
# ══════════════════════════════════════════════════════════════

class TestQaLogAggregation:
    def test_get_daily_qa_counts(self, pg_conn):
        from storage.supabase_store import insert_qa_log, get_daily_qa_counts
        insert_qa_log(_make_log_entry(), pg_conn)
        counts = get_daily_qa_counts(30, 0, pg_conn)
        assert sum(c["count"] for c in counts) >= 1

    def test_get_daily_qa_counts_empty(self):
        # 실제 운영 DB의 qa_log에는 이미 데이터가 쌓여 있어 "결과 없음"을
        # 실제 DB로 재현할 수 없으므로 cursor를 mock하여 빈 결과 분기를 검증합니다.
        from storage.supabase_store import get_daily_qa_counts
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        assert get_daily_qa_counts(30, 0, mock_conn) == []

    def test_get_daily_qa_counts_pagination(self, pg_conn):
        from storage.supabase_store import insert_qa_log, get_daily_qa_counts
        insert_qa_log(_make_log_entry(), pg_conn)
        page1 = get_daily_qa_counts(1, 0, pg_conn)
        assert len(page1) == 1

    def test_get_qa_logs_paginated(self, pg_conn):
        from storage.supabase_store import insert_qa_log, get_qa_logs_paginated
        for _ in range(3):
            insert_qa_log(_make_log_entry(), pg_conn)
        page1 = get_qa_logs_paginated(2, 0, pg_conn)
        assert len(page1) == 2

    def test_get_logs_by_failure_causes_filters_correctly(self, pg_conn):
        from storage.supabase_store import insert_qa_log, get_logs_by_failure_causes
        entry_incomplete = _make_log_entry(failure_cause="검색실패")
        entry_unresolved = _make_log_entry(failure_cause="지식DB공백")
        insert_qa_log(entry_incomplete, pg_conn)
        insert_qa_log(entry_unresolved, pg_conn)
        incomplete = get_logs_by_failure_causes(["검색실패", "질문모호성"], limit=200, conn=pg_conn)
        ids = {log["log_id"] for log in incomplete}
        assert entry_incomplete["log_id"] in ids
        assert entry_unresolved["log_id"] not in ids
        assert all(log["failure_cause"] in ("검색실패", "질문모호성") for log in incomplete)

    def test_get_logs_by_failure_causes_excludes_resolved(self, pg_conn):
        # action_status='완료'로 표시된 항목은 목록에서 제외되어야 하지만,
        # qa_log 행 자체는 지워지지 않아 통계(get_failure_cause_counts 등)에는 남습니다.
        from storage.supabase_store import insert_qa_log, get_logs_by_failure_causes
        from storage.action_store import set_status
        entry = _make_log_entry(failure_cause="검색실패")
        insert_qa_log(entry, pg_conn)
        set_status(entry["log_id"], "완료", pg_conn)
        incomplete = get_logs_by_failure_causes(["검색실패", "질문모호성"], limit=200, conn=pg_conn)
        assert entry["log_id"] not in {log["log_id"] for log in incomplete}

    def test_delete_old_qa_logs_removes_only_stale_rows(self, pg_conn):
        from storage.supabase_store import insert_qa_log, delete_old_qa_logs
        old_entry = _make_log_entry(timestamp=datetime.now(timezone.utc) - timedelta(days=400))
        recent_entry = _make_log_entry(timestamp=datetime.now(timezone.utc) - timedelta(days=10))
        insert_qa_log(old_entry, pg_conn)
        insert_qa_log(recent_entry, pg_conn)

        deleted = delete_old_qa_logs(365, pg_conn)

        assert deleted >= 1
        with pg_conn.cursor() as cur:
            cur.execute("SELECT log_id FROM qa_log WHERE log_id = %s", (old_entry["log_id"],))
            assert cur.fetchone() is None
            cur.execute("SELECT log_id FROM qa_log WHERE log_id = %s", (recent_entry["log_id"],))
            assert cur.fetchone() is not None

    def test_delete_old_qa_logs_also_removes_action_status_to_avoid_fk_violation(self, pg_conn):
        from storage.supabase_store import insert_qa_log, delete_old_qa_logs
        from storage.action_store import set_status
        old_entry = _make_log_entry(
            failure_cause="검색실패", timestamp=datetime.now(timezone.utc) - timedelta(days=400),
        )
        insert_qa_log(old_entry, pg_conn)
        set_status(old_entry["log_id"], "완료", pg_conn)

        delete_old_qa_logs(365, pg_conn)  # FK 위반 없이 통과해야 함

        with pg_conn.cursor() as cur:
            cur.execute("SELECT log_id FROM action_status WHERE log_id = %s", (old_entry["log_id"],))
            assert cur.fetchone() is None

    def test_get_failure_cause_counts(self, pg_conn):
        from storage.supabase_store import insert_qa_log, get_failure_cause_counts
        insert_qa_log(_make_log_entry(failure_cause="정책밖요청"), pg_conn)
        counts = get_failure_cause_counts(pg_conn)
        assert counts["정책밖요청"] == 1


# ══════════════════════════════════════════════════════════════
# tone_config.py / tone_matrix.py 외부화
# ══════════════════════════════════════════════════════════════

class TestToneOverride:
    def test_build_brand_tone_guideline_default(self):
        from tone_config import build_brand_tone_guideline, BRAND_TONE_ELEMENTS
        guideline = build_brand_tone_guideline()
        assert BRAND_TONE_ELEMENTS["persona"] in guideline

    def test_build_brand_tone_guideline_override(self):
        from tone_config import build_brand_tone_guideline
        custom = {"personality": "엄격한 말투를 사용합니다."}
        guideline = build_brand_tone_guideline(custom)
        assert "엄격한 말투를 사용합니다." in guideline

    def test_tone_matrix_builder_uses_override(self):
        from tone_matrix import ToneMatrixBuilder, Situation
        builder = ToneMatrixBuilder(tone_elements={"personality": "커스텀 성격입니다."})
        instruction = builder.build_instruction(Situation.NORMAL_RESPONSE)
        assert "커스텀 성격입니다." in instruction

    def test_tone_matrix_builder_default_when_none(self):
        from tone_matrix import ToneMatrixBuilder, Situation
        from tone_config import BRAND_TONE_ELEMENTS
        builder = ToneMatrixBuilder()
        instruction = builder.build_instruction(Situation.NORMAL_RESPONSE)
        assert BRAND_TONE_ELEMENTS["persona"] in instruction

    def test_situation_classifier_uses_keyword_override(self):
        from tone_matrix import SituationClassifier, SituationClassificationInput, Situation
        clf = SituationClassifier(keywords={"policy_violation": ["대신 출석"]})
        inp = SituationClassificationInput(
            question="친구가 대신 출석 체크해줘도 되나요?", keywords=["출석"],
            question_category="미분류", top_result_category="미분류", repeated_count=0,
        )
        assert clf.classify(inp) == Situation.POLICY_VIOLATION

    def test_situation_classifier_override_excludes_file_keywords(self):
        # override를 주면 situation_keywords.json 파일 키워드는 전혀 쓰이지 않아야 함
        from tone_matrix import SituationClassifier, SituationClassificationInput, Situation
        clf = SituationClassifier(keywords={"policy_violation": []})
        inp = SituationClassificationInput(
            question="현금으로 따로 받을 수 있나요?", keywords=["현금"],
            question_category="미분류", top_result_category="미분류", repeated_count=0,
        )
        assert clf.classify(inp) != Situation.POLICY_VIOLATION

    def test_load_forbidden_words_uses_override(self):
        from chatbot_engine import _load_forbidden_words, contains_forbidden_word
        words = _load_forbidden_words({"custom": ["테스트금지어"]})
        assert contains_forbidden_word("테스트금지어 포함된 문장", words) is True
        assert contains_forbidden_word("씨발", words) is False  # override 사용 시 파일 기본값은 무시

    def test_load_forbidden_words_default_when_none(self):
        from chatbot_engine import _load_forbidden_words, contains_forbidden_word
        words = _load_forbidden_words()
        assert contains_forbidden_word("씨발 진짜", words) is True


# ══════════════════════════════════════════════════════════════
# api/admin.py
# ══════════════════════════════════════════════════════════════

class TestAdminApi:
    @pytest.fixture
    def operator(self, pg_conn):
        from auth import hash_password
        from storage.admin_store import create_operator
        email = f"ops-{uuid.uuid4()}@test.com"
        create_operator(email, hash_password("correct-password-123"), pg_conn)
        return email

    @pytest.fixture
    def settings_seeded(self, pg_conn):
        # app_settings는 실제 운영 행이 항상 이미 존재하는 싱글톤 테이블입니다.
        # 테스트가 끝나면 반드시 원래 운영 설정값으로 복원해 운영 데이터를
        # 오염시키지 않습니다(이전에 이 부분을 빠뜨려 실제 app_settings가
        # 테스트 값으로 덮어써진 사고가 있었습니다).
        from storage.settings_store import seed_default_settings, get_settings, update_settings
        test_values = {
            "operation_team": {"name": "팀", "address": "주소", "phone": "000-0000-0000",
                                "email_list": ["a@b.com"], "operating_hours": "평일 9-18시"},
            "similarity_threshold": 0.55, "search_weights": {"vector": 0.6, "bm25": 0.4},
            "search_top_k": 5, "repeat_threshold": 2, "min_keywords_for_clarity": 1,
            "max_question_length": 500, "rate_limit_per_minute": 10,
            "tone_elements": _TONE_TEST_VALUES,
        }
        try:
            original = get_settings(pg_conn)
        except RuntimeError:
            original = None
        if original is None:
            seed_default_settings(test_values, pg_conn)
        else:
            update_settings(test_values, pg_conn)
        yield
        if original is not None:
            update_settings(original, pg_conn)

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        import api.admin as admin_module
        monkeypatch.setattr(admin_module, "_static_config", {
            "notion_pages": {"faq": "https://notion.so/faq-page"},
        })
        return TestClient(admin_module.app)

    def auth_header(self, email):
        from auth import create_access_token
        return {"Authorization": "Bearer " + create_access_token(email)}

    # ── 로그인/비밀번호 변경 ──────────────────────────────────────

    def test_login_success(self, client, operator):
        res = client.post("/login", json={"email": operator, "password": "correct-password-123"})
        assert res.status_code == 200
        assert res.json()["access_token"]

    def test_login_wrong_password_returns_401(self, client, operator):
        res = client.post("/login", json={"email": operator, "password": "wrong"})
        assert res.status_code == 401

    def test_login_unknown_email_returns_401(self, client):
        res = client.post("/login", json={"email": "nobody@test.com", "password": "x"})
        assert res.status_code == 401

    def test_change_password_requires_auth(self, client):
        res = client.post("/change-password", json={"current_password": "a", "new_password": "bbbbbbbb"})
        assert res.status_code == 401

    def test_change_password_success(self, client, operator):
        res = client.post(
            "/change-password",
            json={"current_password": "correct-password-123", "new_password": "new-password-456"},
            headers=self.auth_header(operator),
        )
        assert res.status_code == 200
        # 새 비밀번호로 다시 로그인되는지 확인
        res2 = client.post("/login", json={"email": operator, "password": "new-password-456"})
        assert res2.status_code == 200

    def test_change_password_wrong_current_password_returns_403(self, client, operator):
        # 401이 아니라 403이어야 합니다: 프론트엔드 api() 헬퍼가 401을 받으면
        # 무조건 로그아웃 처리하는데, 비밀번호 오타는 유효한 세션 안의 입력
        # 오류일 뿐 인증 실패가 아니므로 로그아웃되면 안 됩니다.
        res = client.post(
            "/change-password",
            json={"current_password": "wrong", "new_password": "new-password-456"},
            headers=self.auth_header(operator),
        )
        assert res.status_code == 403

    def test_change_password_too_short_returns_400(self, client, operator):
        res = client.post(
            "/change-password",
            json={"current_password": "correct-password-123", "new_password": "short"},
            headers=self.auth_header(operator),
        )
        assert res.status_code == 400

    # ── 모니터링 ────────────────────────────────────────────────

    def test_daily_counts_requires_auth(self, client):
        assert client.get("/monitoring/daily-counts").status_code == 401

    def test_daily_counts_success(self, client, operator, pg_conn):
        from storage.supabase_store import insert_qa_log
        insert_qa_log(_make_log_entry(), pg_conn)
        res = client.get("/monitoring/daily-counts", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert "daily_counts" in res.json()

    def test_daily_counts_invalid_limit_returns_400(self, client, operator):
        res = client.get("/monitoring/daily-counts?limit=0", headers=self.auth_header(operator))
        assert res.status_code == 400

    def test_qa_logs_invalid_limit_returns_400(self, client, operator):
        res = client.get("/monitoring/qa-logs?limit=0", headers=self.auth_header(operator))
        assert res.status_code == 400

    def test_qa_logs_success_empty(self, client, operator):
        res = client.get("/monitoring/qa-logs", headers=self.auth_header(operator))
        assert res.status_code == 200

    # ── 조치관리 ────────────────────────────────────────────────

    def test_incomplete_answers_filters_correctly(self, client, operator, pg_conn):
        from storage.supabase_store import insert_qa_log
        insert_qa_log(_make_log_entry(failure_cause="검색실패"), pg_conn)
        insert_qa_log(_make_log_entry(failure_cause="지식DB공백"), pg_conn)
        res = client.get("/actions/incomplete", headers=self.auth_header(operator))
        assert res.status_code == 200
        causes = {log["failure_cause"] for log in res.json()["logs"]}
        assert causes <= {"검색실패", "질문모호성"}

    def test_unresolved_answers_filters_correctly(self, client, operator, pg_conn):
        from storage.supabase_store import insert_qa_log
        insert_qa_log(_make_log_entry(failure_cause="정책밖요청"), pg_conn)
        res = client.get("/actions/unresolved", headers=self.auth_header(operator))
        assert res.status_code == 200
        for log in res.json()["logs"]:
            assert log["failure_cause"] in ("지식DB공백", "정책밖요청")

    def test_resolve_action_success(self, client, operator, pg_conn):
        from storage.supabase_store import insert_qa_log
        entry = _make_log_entry(failure_cause="검색실패")
        insert_qa_log(entry, pg_conn)
        res = client.delete(f"/actions/{entry['log_id']}", headers=self.auth_header(operator))
        assert res.status_code == 200

    def test_resolve_action_removes_from_incomplete_list(self, client, operator, pg_conn):
        from storage.supabase_store import insert_qa_log
        entry = _make_log_entry(failure_cause="검색실패")
        insert_qa_log(entry, pg_conn)
        client.delete(f"/actions/{entry['log_id']}", headers=self.auth_header(operator))
        res = client.get("/actions/incomplete", headers=self.auth_header(operator))
        ids = {log["log_id"] for log in res.json()["logs"]}
        assert entry["log_id"] not in ids

    def test_resolve_action_nonexistent_log_id_returns_404(self, client, operator):
        res = client.delete("/actions/nonexistent-log-id", headers=self.auth_header(operator))
        assert res.status_code == 404

    def test_failure_report_includes_all_four_causes(self, client, operator):
        res = client.get("/actions/failure-report", headers=self.auth_header(operator))
        assert res.status_code == 200
        counts = res.json()["counts"]
        assert set(counts.keys()) == {"지식DB공백", "검색실패", "질문모호성", "정책밖요청"}

    # ── 운영설정 ────────────────────────────────────────────────

    def test_get_settings_success(self, client, operator, settings_seeded):
        res = client.get("/settings", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert res.json()["similarity_threshold"] == 0.55

    def test_update_operation_team_success(self, client, operator, settings_seeded):
        res = client.put("/settings/operation-team", headers=self.auth_header(operator), json={
            "name": "새팀", "address": "새주소", "phone": "02-0000-0000",
            "email_list": ["new@test.com"], "operating_hours": "24시간",
        })
        assert res.status_code == 200
        assert res.json()["operation_team"]["name"] == "새팀"

    def test_update_tone_success_reflected_immediately(self, client, operator, settings_seeded):
        res = client.put("/settings/tone", headers=self.auth_header(operator), json={
            "personality": "새로운 성격", "language_purity": "x", "vip_consistency": "x",
            "formality": "x", "channel": "x", "emotional_labor": "x", "persona": "x", "factuality": "x",
        })
        assert res.status_code == 200
        assert res.json()["tone_elements"]["personality"] == "새로운 성격"

    def test_update_situation_keywords_success(self, client, operator, settings_seeded):
        res = client.put("/settings/situation-keywords", headers=self.auth_header(operator), json={
            "policy_violation": ["대신 출석", "대신 출석"],  # 중복 줄 정리 확인
            "escalation_request": ["상담원"], "gratitude": [" 감사합니다 "],  # 공백 정리 확인
            "simple_rejection": ["", "안 할래요", "  "],  # 빈 줄 정리 확인
        })
        assert res.status_code == 200
        body = res.json()
        assert body["situation_keywords"]["policy_violation"] == ["대신 출석"]
        assert body["situation_keywords"]["gratitude"] == ["감사합니다"]
        assert body["situation_keywords"]["simple_rejection"] == ["안 할래요"]

    def test_update_situation_keywords_missing_category_returns_422(self, client, operator, settings_seeded):
        res = client.put("/settings/situation-keywords", headers=self.auth_header(operator), json={
            "policy_violation": ["대신 출석"],
        })
        assert res.status_code == 422

    def test_update_forbidden_words_success(self, client, operator, settings_seeded):
        res = client.put("/settings/forbidden-words", headers=self.auth_header(operator), json={
            "profanity": ["욕설1"], "hate_speech": [], "threats": ["협박1"],
        })
        assert res.status_code == 200
        assert res.json()["forbidden_words"]["profanity"] == ["욕설1"]
        assert res.json()["forbidden_words"]["hate_speech"] == []

    def test_update_forbidden_words_reflected_in_engine_immediately(self, client, operator, settings_seeded):
        res = client.put("/settings/forbidden-words", headers=self.auth_header(operator), json={
            "profanity": ["테스트금지어다"], "hate_speech": [], "threats": [],
        })
        assert res.status_code == 200
        from chatbot_engine import _load_forbidden_words, contains_forbidden_word
        from storage.settings_store import get_settings
        words = _load_forbidden_words(get_settings()["forbidden_words"])
        assert contains_forbidden_word("테스트금지어다 포함된 문장", words) is True

    def test_update_api_params_invalid_returns_400(self, client, operator, settings_seeded):
        res = client.put("/settings/api-params", headers=self.auth_header(operator),
                          json={"max_question_length": 0, "rate_limit_per_minute": 10})
        assert res.status_code == 400

    def test_update_api_params_success(self, client, operator, settings_seeded):
        res = client.put("/settings/api-params", headers=self.auth_header(operator),
                          json={"max_question_length": 300, "rate_limit_per_minute": 5})
        assert res.status_code == 200
        assert res.json()["max_question_length"] == 300

    # ── Knowledge Base ──────────────────────────────────────────

    def test_list_documents_success(self, client, operator):
        res = client.get("/kb/documents", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert "documents" in res.json()

    def test_delete_document_notion_source_forbidden(self, client, operator, pg_conn):
        from models.document import Document
        from storage.supabase_store import upsert_document
        doc = Document.new(source_type="notion", source_origin="FAQ", title="T", content="C",
                            notion_block_id="abc", is_editable=False)
        upsert_document(doc, pg_conn)
        res = client.delete(f"/kb/documents/{doc.doc_id}", headers=self.auth_header(operator))
        assert res.status_code == 403

    def test_delete_document_editable_succeeds(self, client, operator, pg_conn):
        from models.document import Document
        from storage.supabase_store import upsert_document
        doc = Document.new(source_type="docx", source_origin="a.docx", title="T", content="C")
        upsert_document(doc, pg_conn)
        res = client.delete(f"/kb/documents/{doc.doc_id}", headers=self.auth_header(operator))
        assert res.status_code == 200

    def test_delete_document_not_found_returns_404(self, client, operator):
        res = client.delete("/kb/documents/nonexistent-id", headers=self.auth_header(operator))
        assert res.status_code == 404

    def test_upload_unsupported_extension_returns_400(self, client, operator):
        res = client.post("/kb/upload", headers=self.auth_header(operator),
                           files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")})
        assert res.status_code == 400

    def test_upload_hwp_returns_explicit_message(self, client, operator):
        res = client.post("/kb/upload", headers=self.auth_header(operator),
                           files={"file": ("notes.hwp", io.BytesIO(b"x"), "application/octet-stream")})
        assert res.status_code == 400
        assert "PDF" in res.json()["detail"]

    def test_upload_docx_success(self, client, operator):
        from docx import Document as DocxDocument
        buf = io.BytesIO()
        d = DocxDocument()
        d.add_heading("테스트 제목", level=1)
        d.add_paragraph("테스트 내용입니다.")
        d.save(buf)
        buf.seek(0)
        res = client.post("/kb/upload", headers=self.auth_header(operator),
                           files={"file": ("upload-test.docx", buf,
                                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert res.status_code == 200
        assert res.json()["inserted"] >= 1

    def test_embed_all_no_pending_documents_returns_zero(self, client, operator):
        with patch("storage.supabase_store.get_documents_missing_embedding", return_value=[]):
            res = client.post("/kb/documents/embed-all", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "embedded": 0, "failed": 0}

    def test_embed_all_skips_notion_documents(self, client, operator):
        from models.document import Document
        notion_doc = Document.new(source_type="notion", source_origin="FAQ", title="T",
                                   content="C", notion_block_id="abc", is_editable=False)
        fake_provider = MagicMock()
        with patch("storage.supabase_store.get_documents_missing_embedding",
                   return_value=[notion_doc]), \
             patch("embedding_manager.get_embedding_provider", return_value=fake_provider):
            res = client.post("/kb/documents/embed-all", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "embedded": 0, "failed": 0}
        fake_provider.embed_documents.assert_not_called()

    def test_embed_all_embeds_pending_editable_documents(self, client, operator):
        from models.document import Document
        doc = Document.new(source_type="google_sheet", source_origin="시트", title="질문1",
                            content="답변1")
        fake_provider = MagicMock()
        fake_provider.embed_documents.return_value = [[0.1, 0.2]]
        with patch("storage.supabase_store.get_documents_missing_embedding",
                   return_value=[doc]), \
             patch("embedding_manager.get_embedding_provider", return_value=fake_provider), \
             patch("storage.supabase_store.update_embedding") as fake_update:
            res = client.post("/kb/documents/embed-all", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "embedded": 1, "failed": 0}
        fake_update.assert_called_once()

    def test_embed_all_batch_failure_counts_as_failed_without_crashing(self, client, operator):
        from models.document import Document
        doc = Document.new(source_type="google_sheet", source_origin="시트", title="질문1",
                            content="답변1")
        fake_provider = MagicMock()
        fake_provider.embed_documents.side_effect = ConnectionError("voyage api down")
        with patch("storage.supabase_store.get_documents_missing_embedding",
                   return_value=[doc]), \
             patch("embedding_manager.get_embedding_provider", return_value=fake_provider), \
             patch("storage.supabase_store.update_embedding") as fake_update:
            res = client.post("/kb/documents/embed-all", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "embedded": 0, "failed": 1}
        fake_update.assert_not_called()

    def test_google_sheet_empty_url_returns_400(self, client, operator):
        res = client.post("/kb/google-sheet", headers=self.auth_header(operator), json={"url": ""})
        assert res.status_code == 400

    def test_google_sheet_collector_failure_returns_502(self, client, operator):
        with patch("collectors.google_sheet_collector.collect_google_sheet",
                   side_effect=ConnectionError("network down")):
            res = client.post("/kb/google-sheet", headers=self.auth_header(operator),
                               json={"url": "https://docs.google.com/spreadsheets/d/x"})
        assert res.status_code == 502

    def test_notion_refresh_success(self, client, operator, pg_conn):
        fake_result = {"status": "ok", "total_collected": 1, "inserted": 1,
                        "pages": {"faq": {"page_name": "FAQ", "doc_count": 1, "skipped": False}},
                        "validation": {}}
        with patch("api.sync_notion._perform_sync", return_value=fake_result):
            res = client.post("/kb/notion/refresh", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert "FAQ" in res.json()["summary_text"]

    def test_notion_refresh_missing_token_returns_503(self, client, operator):
        with patch("api.sync_notion._perform_sync", side_effect=EnvironmentError("NOTION_API_TOKEN 없음")):
            res = client.post("/kb/notion/refresh", headers=self.auth_header(operator))
        assert res.status_code == 503

    def test_notion_refresh_generic_failure_returns_502(self, client, operator):
        with patch("api.sync_notion._perform_sync", side_effect=ConnectionError("notion api down")):
            res = client.post("/kb/notion/refresh", headers=self.auth_header(operator))
        assert res.status_code == 502

    def test_notion_last_sync_no_record(self, client, operator):
        # sync_metadata는 한 번이라도 갱신(수동/자동)이 발생하면 영구히 행이 남는
        # 실제 운영 테이블이라 "갱신 기록이 아예 없음"을 실제 DB로 재현할 수
        # 없으므로 get_connection을 mock하여 빈 결과 분기를 검증합니다.
        from unittest.mock import patch as _patch
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        with _patch("storage.supabase_store.get_connection", return_value=mock_conn):
            res = client.get("/kb/notion/last-sync", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert res.json()["last_synced_at"] is None
        mock_conn.close.assert_called_once()

    def test_notion_last_sync_after_manual_refresh(self, client, operator, pg_conn):
        fake_result = {"status": "ok", "total_collected": 0, "inserted": 0, "pages": {}, "validation": {}}
        with patch("api.sync_notion._perform_sync", return_value=fake_result):
            client.post("/kb/notion/refresh", headers=self.auth_header(operator))
        res = client.get("/kb/notion/last-sync", headers=self.auth_header(operator))
        assert res.json()["mode"] == "수동"

    def test_notion_faq_url_configured(self, client, operator):
        res = client.get("/kb/notion-faq-url", headers=self.auth_header(operator))
        assert res.json()["url"] == "https://notion.so/faq-page"

    def test_notion_faq_url_placeholder_returns_none(self, client, operator, monkeypatch):
        import api.admin as admin_module
        monkeypatch.setattr(admin_module, "_static_config", {"notion_pages": {"faq": "{{NOTION_FAQ_URL}}"}})
        res = client.get("/kb/notion-faq-url", headers=self.auth_header(operator))
        assert res.json()["url"] is None

    def test_manual_source_guide_success(self, client, operator):
        res = client.get("/kb/manual-source-guide", headers=self.auth_header(operator))
        assert res.status_code == 200
        assert len(res.json()["guide"]) == 5


# ══════════════════════════════════════════════════════════════
# create_admin_account.py
# ══════════════════════════════════════════════════════════════

class TestCreateAdminAccount:
    def test_main_creates_account(self, monkeypatch, pg_conn, capsys):
        import create_admin_account
        email = f"cli-{uuid.uuid4()}@test.com"
        monkeypatch.setattr(sys, "argv", ["create_admin_account.py", email, "password1234"])
        create_admin_account.main()
        from storage.admin_store import get_operator_by_email
        assert get_operator_by_email(email, pg_conn) is not None
        assert "생성되었습니다" in capsys.readouterr().out

    def test_main_duplicate_email_exits(self, monkeypatch, pg_conn):
        import create_admin_account
        from storage.admin_store import create_operator
        email = f"cli-dup-{uuid.uuid4()}@test.com"
        create_operator(email, "x", pg_conn)
        monkeypatch.setattr(sys, "argv", ["create_admin_account.py", email, "password1234"])
        with pytest.raises(SystemExit):
            create_admin_account.main()

    def test_main_short_password_exits(self, monkeypatch):
        import create_admin_account
        monkeypatch.setattr(sys, "argv", ["create_admin_account.py", "x@test.com", "short"])
        with pytest.raises(SystemExit):
            create_admin_account.main()

    def test_main_missing_args_exits(self, monkeypatch):
        import create_admin_account
        monkeypatch.setattr(sys, "argv", ["create_admin_account.py"])
        with pytest.raises(SystemExit):
            create_admin_account.main()
