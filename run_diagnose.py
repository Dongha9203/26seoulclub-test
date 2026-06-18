"""임시 진단 스크립트 — 실제 API 호출용"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from collectors.notion_collector import extract_page_id, _fetch_all_blocks, _chunk_page_blocks
from notion_client import Client
from collections import Counter

token = os.environ["NOTION_API_TOKEN"]
client = Client(auth=token)

url = "https://app.notion.com/p/AX-578c65092af983818231010f842df59b?source=copy_link"
page_id = extract_page_id(url)
print("추출된 page_id:", page_id)

# 블록 전체 조회
blocks = _fetch_all_blocks(client, page_id)
print("전체 블록 수:", len(blocks))

# 블록 타입 분포
type_counter = Counter(b["type"] for b in blocks)
print()
print("[블록 타입 분포]")
for btype, cnt in type_counter.most_common():
    print(f"  {btype:<35} {cnt}개")

# 각 블록 미리보기
print()
print("[블록 목록 (최대 30개)]")
for i, b in enumerate(blocks[:30]):
    btype = b["type"]
    type_data = b.get(btype, {})
    rich_texts = type_data.get("rich_text", [])
    text = "".join(rt.get("plain_text", "") for rt in rich_texts)[:50]
    has_ch = b.get("has_children", False)
    bid = b["id"][:8]
    print(f"  [{i+1:>2}] {btype:<22} id={bid}...  children={has_ch}  text={repr(text)}")
if len(blocks) > 30:
    print(f"  ... 이후 {len(blocks)-30}개 생략")

# 청킹 결과
print()
print("[청킹 적용 결과]")
docs = _chunk_page_blocks(client, blocks, "테스트페이지", url)
print(f"생성된 Document 수: {len(docs)}개")

toggle_src = sum(1 for b in blocks if b["type"] == "toggle")
heading_src = sum(1 for b in blocks if b["type"] in {"heading_1", "heading_2", "heading_3"})
print(f"  toggle 블록: {toggle_src}개 → 1단계 처리")
print(f"  heading 블록: {heading_src}개 → 2단계 처리")
if toggle_src == 0 and heading_src == 0:
    print("  ⚠ 구조 없음 → fallback 분할 적용")

print()
print("[Document 목록]")
for i, doc in enumerate(docs):
    bid = doc.notion_block_id or "MISSING"
    deeplink = doc.deep_link_url() or "없음"
    print(f"  [{i+1:>2}] [{doc.category}]")
    print(f"       title   : {doc.title[:55]}")
    print(f"       content : {doc.content[:60].replace(chr(10), ' ')!r}")
    print(f"       block_id: {bid}")
    print(f"       deeplink: {deeplink[:70]}")

# 블록 ID 검증
missing = [d for d in docs if not d.notion_block_id]
print()
print(f"[블록 ID 검증] 누락={len(missing)}개 / 전체={len(docs)}개")
if missing:
    for d in missing:
        print(f"  ⚠ 누락: {d.title}")
