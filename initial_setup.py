"""
초기 데이터 구축 스크립트.

개발자가 1회 실행하는 스크립트입니다.
노션 3페이지 + config.json에 등록된 구글 스프레드시트를 한 번에 수집해 Supabase Postgres에 적재합니다.

실행 방법:
  python initial_setup.py

전제 조건:
  1. .env 파일에 NOTION_API_TOKEN이 설정되어 있어야 합니다.
  2. config.json의 notion_pages에 실제 URL이 설정되어 있어야 합니다.
"""

import json
import logging
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("initial_setup")


def print_separator(char="─", width=60):
    print(char * width)


def main():
    print_separator("═")
    print("  동아리ON 챗봇 — 지식DB 초기 구축 스크립트")
    print_separator("═")

    # config.json 로드
    config_path = _root / "config.json"
    if not config_path.exists():
        print(f"[오류] config.json을 찾을 수 없습니다: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # DB 초기화
    from storage.supabase_store import initialize_db, delete_by_source_origin, upsert_documents
    print("\n[1/4] Supabase Postgres 데이터베이스 초기화...")
    initialize_db()
    print("  ✓ 테이블 생성 완료")

    all_docs = []

    # ── 노션 3페이지 수집 ──────────────────────────────────────────────────
    print("\n[2/4] 노션 페이지 수집...")
    notion_pages = config.get("notion_pages", {})
    configured_pages = {k: v for k, v in notion_pages.items() if v and "{{" not in v}

    if not configured_pages:
        print("  ⚠ config.json에 노션 페이지 URL이 설정되어 있지 않습니다.")
        print("    config.json의 notion_pages 값을 실제 URL로 교체 후 재실행하세요.")
    else:
        from collectors.notion_collector import sync_notion_pages
        try:
            notion_docs, notion_summary = sync_notion_pages(config)
            all_docs.extend(notion_docs)

            page_name_map = {
                "main": "메인페이지",
                "integrated_system": "통합시스템",
                "faq": "FAQ",
            }
            for key, info in notion_summary.items():
                page_name = page_name_map.get(key, key)
                if info.get("skipped"):
                    print(f"  ✗ {page_name}: 건너뜀 ({info.get('reason', '')})")
                else:
                    print(f"  ✓ {page_name}: {info['doc_count']}개 Document 수집")
                    # 기존 데이터 삭제 후 새 데이터 저장
                    delete_by_source_origin(page_name)

        except EnvironmentError as e:
            print(f"  ✗ 노션 수집 실패: {e}")
            print("    .env 파일에 NOTION_API_TOKEN을 설정하세요.")

    # ── 구글 스프레드시트 수집 ──────────────────────────────────────────────
    print("\n[3/4] 구글 스프레드시트 수집...")
    google_sheets = config.get("google_sheets", [])

    if not google_sheets:
        print("  ℹ config.json의 google_sheets가 비어있습니다. (건너뜁니다)")
    else:
        from collectors.google_sheet_collector import collect_google_sheet
        for sheet_url in google_sheets:
            if not sheet_url or "{{" in sheet_url:
                print(f"  ⚠ URL이 설정되지 않은 항목을 건너뜁니다: {sheet_url}")
                continue
            try:
                sheet_docs = collect_google_sheet(sheet_url)
                all_docs.extend(sheet_docs)
                # 기존 데이터 삭제
                if sheet_docs:
                    delete_by_source_origin(sheet_docs[0].source_origin)
                print(f"  ✓ {sheet_url[:60]}...: {len(sheet_docs)}개 Document 수집")
            except ValueError as e:
                print(f"  ✗ 구글시트 수집 실패: {e}")

    # ── Supabase Postgres 저장 ────────────────────────────────────────────
    print("\n[4/4] Supabase Postgres에 저장...")
    if all_docs:
        inserted = upsert_documents(all_docs)
        print(f"  ✓ {inserted}개 Document 저장 완료")
    else:
        print("  ℹ 저장할 Document가 없습니다.")

    # ── 결과 요약 ──────────────────────────────────────────────────────────
    print_separator()
    print("결과 요약")
    print_separator()

    from storage.supabase_store import get_total_count, get_category_distribution
    from utils.validators import validate_notion_block_ids

    total = get_total_count()
    print(f"  총 Document 수: {total}개")

    print("\n  [카테고리별 분포]")
    dist = get_category_distribution()
    for cat, cnt in dist.items():
        bar = "█" * min(cnt, 30)
        print(f"  {cat:<20} {cnt:>4}개  {bar}")

    print("\n  [노션 블록 ID 검증]")
    validation = validate_notion_block_ids(all_docs)
    print(f"  노션 Document 수:      {validation['total_notion_docs']}개")
    print(f"  block_id 존재:        {validation['block_id_present']}개")
    print(f"  block_id 누락:        {validation['block_id_missing']}개")
    print(f"  매핑 성공률:           {validation['success_rate_pct']}%")

    if validation["block_id_missing"] > 0:
        print("\n  ⚠ block_id 누락 항목:")
        for title in validation["missing_titles"]:
            print(f"    - {title}")

    print_separator("═")
    print("  초기 구축 완료!")
    print_separator("═")


if __name__ == "__main__":
    main()
