"""
Notion 페이지 수집 모듈.

적응형 청킹 전략 (매 수집 시마다 처음부터 적용):
  1단계 — toggle 우선 분리: toggle 블록 + 자식 전체 → 독립 Document
  2단계 — 나머지는 heading 단위 보완: heading_{1,2,3} 사이 블록 묶음 → Document
  fallback — heading/toggle 모두 없으면 800자 단위 균등 분할 (경고 로그)
"""

import logging
import os
import re
from typing import List, Optional, Tuple

from notion_client import Client
from notion_client.errors import APIResponseError

from models.document import Document
from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

FALLBACK_CHUNK_SIZE = 800
HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _extract_text(block: dict) -> str:
    """블록의 rich_text를 plain text로 추출합니다."""
    block_type = block.get("type", "")
    type_data = block.get(block_type, {})
    rich_texts = type_data.get("rich_text", [])
    return "".join(rt.get("plain_text", "") for rt in rich_texts)


def _fetch_all_blocks(client: Client, block_id: str) -> List[dict]:
    """페이지네이션을 처리해 모든 블록을 반환합니다."""
    blocks = []
    cursor = None
    while True:
        resp = client.blocks.children.list(block_id=block_id, start_cursor=cursor)
        blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return blocks


def _fetch_toggle_children_text(client: Client, block_id: str, depth: int = 0) -> str:
    """toggle 블록의 모든 자식 텍스트를 재귀적으로 수집합니다."""
    if depth > 5:
        return ""
    parts = []
    for child in _fetch_all_blocks(client, block_id):
        text = _extract_text(child)
        if text:
            parts.append(text)
        if child.get("has_children"):
            nested = _fetch_toggle_children_text(client, child["id"], depth + 1)
            if nested:
                parts.append(nested)
    return "\n".join(parts)


def _make_document(
    source_origin: str,
    title: str,
    content: str,
    notion_page_url: str,
    notion_block_id: Optional[str],
) -> Document:
    doc = Document.new(
        source_type="notion",
        source_origin=source_origin,
        title=title or source_origin,
        content=content,
        notion_page_url=notion_page_url,
        notion_block_id=notion_block_id,
        is_editable=False,
    )
    doc.category = tag_category(doc.title, doc.content)
    return doc


# ---------------------------------------------------------------------------
# 청킹 로직
# ---------------------------------------------------------------------------

def _chunk_page_blocks(
    client: Client,
    blocks: List[dict],
    source_origin: str,
    notion_page_url: str,
) -> List[Document]:
    """
    적응형 청킹: toggle 우선 → heading 보완 → fallback
    """
    documents: List[Document] = []
    toggle_ids: set = set()

    # ── 1단계: toggle 블록 우선 분리 ──────────────────────────────────────
    for block in blocks:
        if block["type"] == "toggle":
            toggle_ids.add(block["id"])
            toggle_title = _extract_text(block) or "토글 항목"
            children_text = ""
            if block.get("has_children"):
                children_text = _fetch_toggle_children_text(client, block["id"])
            doc = _make_document(source_origin, toggle_title, children_text,
                                 notion_page_url, block["id"])
            documents.append(doc)

    # ── 2단계: 비-toggle 블록을 heading 단위로 묶기 ───────────────────────
    non_toggle = [b for b in blocks if b["id"] not in toggle_ids]
    has_headings = any(b["type"] in HEADING_TYPES for b in non_toggle)

    if has_headings:
        docs = _chunk_by_headings(non_toggle, source_origin, notion_page_url)
        documents.extend(docs)
    elif non_toggle:
        docs = _chunk_fallback(non_toggle, source_origin, notion_page_url)
        documents.extend(docs)

    return documents


def _chunk_by_headings(
    blocks: List[dict],
    source_origin: str,
    notion_page_url: str,
) -> List[Document]:
    """heading_{1,2,3} 기준으로 블록을 묶어 Document 리스트 반환."""
    documents: List[Document] = []
    current_heading_block: Optional[dict] = None
    current_block_id: Optional[str] = None
    content_parts: List[str] = []

    def flush():
        nonlocal current_heading_block, current_block_id, content_parts
        if current_block_id is None:
            return
        title = (_extract_text(current_heading_block)
                 if current_heading_block else source_origin)
        content = "\n".join(content_parts)
        doc = _make_document(source_origin, title, content,
                             notion_page_url, current_block_id)
        documents.append(doc)
        current_heading_block = None
        current_block_id = None
        content_parts = []

    for block in blocks:
        btype = block["type"]
        if btype in HEADING_TYPES:
            flush()
            current_heading_block = block
            current_block_id = block["id"]
        else:
            if current_block_id is None:
                # heading 이전 블록(서론)
                current_block_id = block["id"]
            text = _extract_text(block)
            if text:
                content_parts.append(text)

    flush()
    return documents


def _chunk_fallback(
    blocks: List[dict],
    source_origin: str,
    notion_page_url: str,
) -> List[Document]:
    """toggle도 heading도 없을 때 800자 단위 분할 (경고 로그 포함)."""
    logger.warning(
        "[FALLBACK 청킹] '%s' 페이지에 heading/toggle이 없습니다. "
        "800자 단위 분할을 적용합니다. 페이지 구조 개선을 권장합니다.",
        source_origin,
    )
    all_parts = [_extract_text(b) for b in blocks if _extract_text(b)]
    full_text = "\n".join(all_parts)

    if not full_text.strip():
        return []

    if len(full_text) <= FALLBACK_CHUNK_SIZE:
        doc = _make_document(source_origin, source_origin, full_text,
                             notion_page_url, blocks[0]["id"])
        return [doc]

    documents = []
    for idx, start in enumerate(range(0, len(full_text), FALLBACK_CHUNK_SIZE)):
        chunk = full_text[start:start + FALLBACK_CHUNK_SIZE]
        block_idx = min(idx, len(blocks) - 1)
        doc = _make_document(
            source_origin,
            f"{source_origin} (파트 {idx + 1})",
            chunk,
            notion_page_url,
            blocks[block_idx]["id"],
        )
        documents.append(doc)
    return documents


# ---------------------------------------------------------------------------
# 공개 인터페이스
# ---------------------------------------------------------------------------

def collect_notion_page(
    client: Client,
    page_id: str,
    page_name: str,
    notion_page_url: str,
) -> List[Document]:
    """
    단일 Notion 페이지를 수집해 Document 리스트를 반환합니다.
    page_id: UUID 형식 또는 하이픈 없는 32자 hex
    """
    logger.info("Notion 수집 시작: %s (id=%s)", page_name, page_id)
    blocks = _fetch_all_blocks(client, page_id)
    logger.info("  블록 수: %d", len(blocks))

    if not blocks:
        logger.warning("  '%s' 페이지가 비어있습니다.", page_name)
        return []

    docs = _chunk_page_blocks(client, blocks, page_name, notion_page_url)
    logger.info("  생성된 Document 수: %d", len(docs))
    return docs


def get_page_last_edited_time(client: Client, page_id: str) -> Optional[str]:
    """페이지의 last_edited_time을 반환합니다 (변경 감지용)."""
    try:
        page = client.pages.retrieve(page_id=page_id)
        return page.get("last_edited_time")
    except APIResponseError as e:
        logger.error("페이지 조회 실패 (id=%s): %s", page_id, e)
        return None


def extract_page_id(url_or_id: str) -> str:
    """
    Notion URL 또는 ID 문자열에서 페이지 ID(UUID 형식)를 추출합니다.

    지원 형식:
      - https://www.notion.so/workspace/Page-Title-abc123def456abc123def456abc123de
      - https://www.notion.so/abc123def456abc123def456abc123de?pvs=4
      - xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (UUID)
      - abc123def456abc123def456abc123de (32자 hex)
    """
    # 쿼리스트링, 앵커 제거
    clean = url_or_id.split("?")[0].split("#")[0].strip()

    # UUID 형식으로 이미 되어있으면 그대로 반환
    uuid_pattern = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.IGNORECASE,
    )
    m = uuid_pattern.search(clean)
    if m:
        return m.group(1).lower()

    # 32자 연속 hex 탐색 (URL 마지막 세그먼트에 있는 경우 포함)
    hex_pattern = re.compile(r"([0-9a-f]{32})(?:[^0-9a-f]|$)", re.IGNORECASE)
    m = hex_pattern.search(clean)
    if m:
        raw = m.group(1).lower()
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"

    # 그 외는 원본 반환 (notion-client가 자체 처리)
    return url_or_id


def sync_notion_pages(config: dict) -> Tuple[List[Document], dict]:
    """
    config.json의 notion_pages에 정의된 3개 페이지를 모두 수집합니다.
    변경 감지(last_edited_time)는 하지 않고 항상 전체 재수집합니다.
    (변경 감지는 cron 엔드포인트에서 별도로 처리합니다.)

    Returns:
        (all_docs, summary)
    """
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        raise EnvironmentError("NOTION_API_TOKEN 환경변수가 설정되지 않았습니다.")

    client = Client(auth=token)
    all_docs: List[Document] = []
    summary: dict = {}

    page_name_map = {
        "main": "메인페이지",
        "integrated_system": "통합시스템",
        "faq": "FAQ",
    }

    for key, url in config.get("notion_pages", {}).items():
        if not url or "{{" in url:
            logger.warning("노션 페이지 '%s'의 URL이 설정되지 않았습니다. 건너뜁니다.", key)
            summary[key] = {"skipped": True, "reason": "URL 미설정"}
            continue

        page_name = page_name_map.get(key, key)
        page_id = extract_page_id(url)

        try:
            docs = collect_notion_page(client, page_id, page_name, url)
            all_docs.extend(docs)
            summary[key] = {
                "page_name": page_name,
                "doc_count": len(docs),
                "skipped": False,
            }
        except APIResponseError as e:
            logger.error("노션 페이지 수집 실패 (%s): %s", page_name, e)
            summary[key] = {
                "page_name": page_name,
                "doc_count": 0,
                "skipped": True,
                "reason": str(e),
            }

    return all_docs, summary


def sync_notion_pages_incremental(config: dict, db_path=None) -> Tuple[List[Document], dict]:
    """
    변경된 페이지만 재수집합니다 (cron 엔드포인트용).
    sync_metadata 테이블의 last_notion_edited_time과 비교합니다.

    Returns:
        (updated_docs, summary)
    """
    from datetime import datetime, timezone
    from storage.sqlite_store import get_sync_metadata, upsert_sync_metadata

    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        raise EnvironmentError("NOTION_API_TOKEN 환경변수가 설정되지 않았습니다.")

    client = Client(auth=token)
    all_docs: List[Document] = []
    summary: dict = {}

    page_name_map = {
        "main": "메인페이지",
        "integrated_system": "통합시스템",
        "faq": "FAQ",
    }

    for key, url in config.get("notion_pages", {}).items():
        if not url or "{{" in url:
            summary[key] = {"skipped": True, "reason": "URL 미설정"}
            continue

        page_name = page_name_map.get(key, key)
        page_id = extract_page_id(url)

        # 마지막 수정 시각 확인
        last_edited = get_page_last_edited_time(client, page_id)
        if last_edited is None:
            summary[key] = {"skipped": True, "reason": "페이지 조회 실패"}
            continue

        # 이전 동기화 기록과 비교
        meta = get_sync_metadata(key, db_path)
        if meta and meta["last_notion_edited_time"] == last_edited:
            logger.info("변경 없음, 스킵: %s (last_edited=%s)", page_name, last_edited)
            summary[key] = {
                "page_name": page_name,
                "doc_count": 0,
                "skipped": True,
                "reason": "변경 없음",
            }
            continue

        try:
            docs = collect_notion_page(client, page_id, page_name, url)
            all_docs.extend(docs)
            now = datetime.now(timezone.utc).isoformat()
            upsert_sync_metadata(key, last_edited, now, db_path)
            summary[key] = {
                "page_name": page_name,
                "doc_count": len(docs),
                "skipped": False,
            }
        except APIResponseError as e:
            logger.error("노션 페이지 수집 실패 (%s): %s", page_name, e)
            summary[key] = {
                "page_name": page_name,
                "doc_count": 0,
                "skipped": True,
                "reason": str(e),
            }

    return all_docs, summary
