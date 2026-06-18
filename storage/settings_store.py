"""
대시보드에서 조정 가능한 운영설정 저장소.

config.json은 코드 배포가 필요한 정적 값(notion_pages, categories 등)만 남기고,
런타임에 즉시 반영돼야 하는 값(담당자 연락처, 신뢰도 threshold, 톤 8요소,
API 운영 파라미터)은 이 테이블(app_settings, 단일 행)에서 관리합니다.
Vercel 서버리스는 파일시스템이 읽기 전용이라 config.json에 저장하면 반영되지 않습니다.
"""

from typing import Dict

import psycopg2.extras

from storage.supabase_store import get_connection, _with_conn

_CREATE_APP_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    id                       INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    operation_team           JSONB NOT NULL,
    similarity_threshold     DOUBLE PRECISION NOT NULL,
    search_weights           JSONB NOT NULL,
    search_top_k             INTEGER NOT NULL,
    repeat_threshold         INTEGER NOT NULL,
    min_keywords_for_clarity INTEGER NOT NULL,
    max_question_length      INTEGER NOT NULL,
    rate_limit_per_minute    INTEGER NOT NULL,
    tone_personality         TEXT NOT NULL,
    tone_language_purity     TEXT NOT NULL,
    tone_vip_consistency     TEXT NOT NULL,
    tone_formality           TEXT NOT NULL,
    tone_channel              TEXT NOT NULL,
    tone_emotional_labor      TEXT NOT NULL,
    tone_persona               TEXT NOT NULL,
    tone_factuality             TEXT NOT NULL,
    situation_keywords          JSONB,
    forbidden_words              JSONB,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# 기존에 이미 배포된 app_settings 테이블(이 두 컬럼이 추가되기 전)에도 적용되도록
# CREATE TABLE과 별도로 ALTER TABLE ADD COLUMN IF NOT EXISTS를 항상 실행합니다.
_ALTER_ADD_KEYWORD_COLUMNS_SQL = """
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS situation_keywords JSONB;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS forbidden_words JSONB;
"""

TONE_KEYS = ["personality", "language_purity", "vip_consistency", "formality",
             "channel", "emotional_labor", "persona", "factuality"]

_SIMPLE_KEYS = ["similarity_threshold", "search_top_k", "repeat_threshold",
                "min_keywords_for_clarity", "max_question_length", "rate_limit_per_minute"]


def initialize_settings_db(conn=None) -> None:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(_CREATE_APP_SETTINGS_SQL)
            cur.execute(_ALTER_ADD_KEYWORD_COLUMNS_SQL)
        c.commit()
    finally:
        if owns_conn:
            c.close()


def seed_default_settings(defaults: Dict, conn=None) -> None:
    """app_settings에 행이 하나도 없을 때만 기본값으로 1행을 만듭니다."""
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM app_settings")
            if cur.fetchone()["cnt"] > 0:
                return
            cur.execute(
                """
                INSERT INTO app_settings (
                    id, operation_team, similarity_threshold, search_weights, search_top_k,
                    repeat_threshold, min_keywords_for_clarity, max_question_length,
                    rate_limit_per_minute, tone_personality, tone_language_purity,
                    tone_vip_consistency, tone_formality, tone_channel, tone_emotional_labor,
                    tone_persona, tone_factuality, situation_keywords, forbidden_words
                ) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    psycopg2.extras.Json(defaults["operation_team"]),
                    defaults["similarity_threshold"],
                    psycopg2.extras.Json(defaults["search_weights"]),
                    defaults["search_top_k"],
                    defaults["repeat_threshold"],
                    defaults["min_keywords_for_clarity"],
                    defaults["max_question_length"],
                    defaults["rate_limit_per_minute"],
                    *[defaults["tone_elements"][k] for k in TONE_KEYS],
                    psycopg2.extras.Json(defaults["situation_keywords"]) if defaults.get("situation_keywords") is not None else None,
                    psycopg2.extras.Json(defaults["forbidden_words"]) if defaults.get("forbidden_words") is not None else None,
                ),
            )
        c.commit()
    finally:
        if owns_conn:
            c.close()


def get_settings(conn=None) -> Dict:
    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM app_settings WHERE id = 1")
            row = cur.fetchone()
    finally:
        if owns_conn:
            c.close()
    if row is None:
        raise RuntimeError("app_settings에 설정 행이 없습니다 (seed_default_settings 먼저 실행 필요).")
    return {
        "operation_team": row["operation_team"],
        "similarity_threshold": row["similarity_threshold"],
        "search_weights": row["search_weights"],
        "search_top_k": row["search_top_k"],
        "repeat_threshold": row["repeat_threshold"],
        "min_keywords_for_clarity": row["min_keywords_for_clarity"],
        "max_question_length": row["max_question_length"],
        "rate_limit_per_minute": row["rate_limit_per_minute"],
        "tone_elements": {k: row[f"tone_{k}"] for k in TONE_KEYS},
        "situation_keywords": row["situation_keywords"],
        "forbidden_words": row["forbidden_words"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def update_settings(partial: Dict, conn=None) -> Dict:
    """partial에 있는 최상위 키만 갱신합니다.
    허용 키: operation_team, similarity_threshold, search_weights, search_top_k,
    repeat_threshold, min_keywords_for_clarity, max_question_length,
    rate_limit_per_minute, tone_elements(dict), situation_keywords(dict), forbidden_words(dict)."""
    set_clauses = []
    params = []

    for key in _SIMPLE_KEYS:
        if key in partial:
            set_clauses.append(f"{key} = %s")
            params.append(partial[key])

    if "operation_team" in partial:
        set_clauses.append("operation_team = %s")
        params.append(psycopg2.extras.Json(partial["operation_team"]))

    if "search_weights" in partial:
        set_clauses.append("search_weights = %s")
        params.append(psycopg2.extras.Json(partial["search_weights"]))

    if "tone_elements" in partial:
        for k, v in partial["tone_elements"].items():
            if k in TONE_KEYS:
                set_clauses.append(f"tone_{k} = %s")
                params.append(v)

    if "situation_keywords" in partial:
        set_clauses.append("situation_keywords = %s")
        params.append(psycopg2.extras.Json(partial["situation_keywords"]))

    if "forbidden_words" in partial:
        set_clauses.append("forbidden_words = %s")
        params.append(psycopg2.extras.Json(partial["forbidden_words"]))

    if not set_clauses:
        return get_settings(conn)

    set_clauses.append("updated_at = NOW()")
    sql = f"UPDATE app_settings SET {', '.join(set_clauses)} WHERE id = 1"

    c, owns_conn = _with_conn(conn)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
        c.commit()
    finally:
        if owns_conn:
            c.close()
    return get_settings(conn)
