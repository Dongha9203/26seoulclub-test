"""
노션 페이지 블록 구조 진단 스크립트 (설계 검증용).

실행 방법:
  python diagnose_notion.py

전제 조건:
  1. .env 파일에 NOTION_API_TOKEN이 설정되어 있어야 합니다.
  2. config.json의 notion_pages에 실제 URL이 설정되어 있어야 합니다.

출력 내용:
  - 각 페이지별 블록 타입 분포
  - 적응형 청킹 적용 시 예상 Document 수
  - 각 Document의 예상 제목 미리보기
  - 블록 ID 매핑 현황
"""

import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

logging.basicConfig(level=logging.WARNING)  # 진단 출력에 집중


def print_sep(char="─", width=70):
    print(char * width)


def diagnose_page(client, page_key: str, page_name: str, url: str):
    from collectors.notion_collector import (
        extract_page_id, _fetch_all_blocks, _fetch_toggle_children_text,
        _chunk_page_blocks,
    )

    print_sep("═")
    print(f"  페이지: {page_name}  ({url[:60]}...)")
    print_sep("═")

    page_id = extract_page_id(url)
    print(f"  추출된 page_id: {page_id}")

    # 전체 블록 조회
    blocks = _fetch_all_blocks(client, page_id)
    print(f"  전체 블록 수: {len(blocks)}")

    # 블록 타입 분포
    type_counter = Counter(b["type"] for b in blocks)
    print("\n  [블록 타입 분포]")
    for btype, cnt in type_counter.most_common():
        print(f"    {btype:<30} {cnt:>4}개")

    # 청킹 시뮬레이션
    docs = _chunk_page_blocks(client, blocks, page_name, url)
    print(f"\n  [청킹 결과: {len(docs)}개 Document 예상]")

    toggle_count = sum(1 for b in blocks if b["type"] == "toggle")
    heading_count = sum(1 for b in blocks if b["type"] in {"heading_1", "heading_2", "heading_3"})
    print(f"    toggle 블록: {toggle_count}개 (1단계)")
    print(f"    heading 블록: {heading_count}개 (2단계)")
    if toggle_count == 0 and heading_count == 0:
        print("    ⚠ toggle/heading 없음 → fallback 800자 분할 적용됨")

    print("\n  [Document 제목 미리보기]")
    for i, doc in enumerate(docs[:20]):
        block_id_disp = f"  [block: {doc.notion_block_id}]" if doc.notion_block_id else "  [block: ⚠ 없음]"
        print(f"    {i+1:>3}. [{doc.category}] {doc.title[:50]}{block_id_disp}")
    if len(docs) > 20:
        print(f"    ... (이후 {len(docs) - 20}개 생략)")

    # 블록 ID 누락 검사
    missing_ids = [d for d in docs if not d.notion_block_id]
    print(f"\n  [블록 ID 검증]")
    print(f"    block_id 존재: {len(docs) - len(missing_ids)}개")
    print(f"    block_id 누락: {len(missing_ids)}개")
    if missing_ids:
        for d in missing_ids:
            print(f"    ⚠ 누락: {d.title[:50]}")

    # 딥링크 예시
    if docs and docs[0].notion_block_id:
        print(f"\n  [딥링크 예시]")
        print(f"    {docs[0].deep_link_url()}")

    print()


def main():
    import os
    token = os.environ.get("NOTION_API_TOKEN")
    if not token or token == "{{NOTION_API_TOKEN}}":
        print("=" * 70)
        print("오류: NOTION_API_TOKEN이 설정되지 않았습니다.")
        print()
        print("다음 단계를 따르세요:")
        print("  1. .env 파일을 열고 NOTION_API_TOKEN에 실제 토큰을 입력하세요.")
        print("  2. Notion 통합(Integration)을 생성하려면:")
        print("     https://www.notion.so/my-integrations")
        print("  3. 각 페이지에 통합을 연결(Connect to integration)하세요.")
        print("=" * 70)
        sys.exit(1)

    config_path = _root / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    notion_pages = config.get("notion_pages", {})
    configured = {k: v for k, v in notion_pages.items() if v and "{{" not in v}

    if not configured:
        print("=" * 70)
        print("오류: config.json에 노션 페이지 URL이 설정되지 않았습니다.")
        print()
        print("config.json의 notion_pages 값을 실제 Notion 페이지 URL로 교체하세요:")
        print('  "main": "https://www.notion.so/your-page-id"')
        print("=" * 70)
        sys.exit(1)

    from notion_client import Client
    client = Client(auth=token)

    page_name_map = {
        "main": "메인페이지",
        "integrated_system": "통합시스템",
        "faq": "FAQ",
    }

    print("\n동아리ON 챗봇 — 노션 페이지 블록 구조 진단")
    print(f"설정된 페이지 수: {len(configured)}개\n")

    for key, url in configured.items():
        page_name = page_name_map.get(key, key)
        try:
            diagnose_page(client, key, page_name, url)
        except Exception as e:
            print(f"[오류] {page_name} 진단 실패: {e}")
            print()

    print_sep("═")
    print("  진단 완료. 위 결과를 Claude Code에 공유하면")
    print("  청킹 전략의 적합성을 확인할 수 있습니다.")
    print_sep("═")


if __name__ == "__main__":
    main()
