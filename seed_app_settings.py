"""
app_settings 초기값 시드 스크립트 (1회 실행).

config.json의 운영설정 값과 tone_config.py의 톤 8요소 기본값을 그대로
app_settings 테이블에 옮겨, 대시보드에서 즉시 조회/수정할 수 있게 합니다.
이미 행이 있으면 아무 것도 하지 않습니다(seed_default_settings가 멱등).

실행 방법:
  python seed_app_settings.py
"""

import json
import sys
from pathlib import Path

_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")


def main():
    with open(_root / "config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
    with open(_root / "situation_keywords.json", "r", encoding="utf-8") as f:
        situation_keywords = json.load(f).get("categories", {})
    with open(_root / "forbidden_words.json", "r", encoding="utf-8") as f:
        forbidden_words = json.load(f).get("categories", {})

    from tone_config import BRAND_TONE_ELEMENTS
    from storage.settings_store import initialize_settings_db, seed_default_settings, get_settings, update_settings

    initialize_settings_db()

    defaults = {
        "operation_team": config.get("operation_team", {}),
        "similarity_threshold": config.get("similarity_threshold", 0.55),
        "search_weights": config.get("search_weights", {"vector": 0.6, "bm25": 0.4}),
        "search_top_k": config.get("search_top_k", 5),
        "repeat_threshold": config.get("repeat_threshold", 2),
        "min_keywords_for_clarity": config.get("min_keywords_for_clarity", 1),
        "max_question_length": config.get("max_question_length", 500),
        "rate_limit_per_minute": config.get("rate_limit_per_minute", 10),
        "tone_elements": dict(BRAND_TONE_ELEMENTS),
        "situation_keywords": situation_keywords,
        "forbidden_words": forbidden_words,
    }
    seed_default_settings(defaults)

    # 이미 배포돼있던 기존 행에는 situation_keywords/forbidden_words 컬럼이 이번에
    # 새로 추가된 것이라 NULL일 수 있습니다 — 비어있을 때만 파일 기본값으로 채웁니다
    # (운영자가 대시보드에서 이미 수정한 값이 있다면 덮어쓰지 않습니다).
    current = get_settings()
    backfill = {}
    if current["situation_keywords"] is None:
        backfill["situation_keywords"] = situation_keywords
    if current["forbidden_words"] is None:
        backfill["forbidden_words"] = forbidden_words
    if backfill:
        update_settings(backfill)

    print("app_settings 시드 완료:")
    print(json.dumps(get_settings(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
