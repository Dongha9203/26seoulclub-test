"""
Notion 페이지 수집 모듈.

적응형 청킹 전략 (매 수집 시마다 처음부터 적용):
  1단계 — toggle 우선 분리: toggle 블록 + 자식 전체 → 독립 Document
  2단계 — 나머지는 heading 단위 보완: heading_{1,2,3} 사이 블록 묶음 → Document
  fallback — heading/toggle 모두 없으면 800자 단위 균등 분할 (경고 로그)

하위 페이지 재귀 수집:
  column_list/column/synced_block처럼 레이아웃만 담당하는 컨테이너와 callout은
  내용을 펼쳐서 위 청킹 로직이 그대로 보게 하고, 그 안에서 발견되는 child_page는
  별도 페이지로 분리해 자신의 공개 URL을 다시 조회한 뒤 재귀적으로 수집합니다.
  child_database(자료실 등)는 블록 API로 내용을 알 수 없어 현재는 건너뜁니다.
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
MAX_NESTING_DEPTH = 5

# 레이아웃 전용 컨테이너: 자신은 버리고 자식만 펼칩니다 (own text 없음).
# table은 자신은 텍스트가 없고 실제 내용은 자식 table_row에 있어 여기 포함합니다.
TRANSPARENT_CONTAINER_TYPES = {"column_list", "column", "synced_block", "table"}
# 자체 텍스트가 있을 수 있는 컨테이너: 자신은 남기고 자식도 이어붙입니다.
TEXT_CONTAINER_TYPES = {"callout"}


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _rich_text_plain(rich_texts: List[dict]) -> str:
    """rich_text 배열을 plain text로 합칩니다.

    보이는 글자와 실제 하이퍼링크가 다른 경우(예: "최종 활동계획 제출(클릭)"라는
    글자 뒤에 Airtable 폼 링크가 숨어있는 흔한 패턴), plain_text만 모으면 그
    링크 자체가 통째로 사라집니다. href가 있고 글자 안에 이미 포함돼 있지 않으면
    괄호로 덧붙여 링크가 검색/답변에서 살아남게 합니다.
    """
    parts = []
    for rt in rich_texts:
        text = rt.get("plain_text", "")
        href = rt.get("href")
        if href and href not in text:
            text = f"{text} ({href})" if text else href
        parts.append(text)
    return "".join(parts)


def _extract_text(block: dict) -> str:
    """블록에서 검색 가능한 텍스트를 추출합니다.

    - table_row: 일반 블록과 달리 rich_text가 셀 단위 2차원 배열(cells)로 들어있어
      따로 처리합니다.
    - embed/bookmark/link_preview처럼 rich_text가 없고 url만 있는 블록(Airtable 등
      외부 서비스 임베드)은 내용을 직접 읽을 수 없으므로, 대신 그 링크를 텍스트로
      남겨 검색에 걸리게 합니다 ("관련 링크: https://...").
    """
    block_type = block.get("type", "")
    type_data = block.get(block_type, {})

    if block_type == "table_row":
        cells = type_data.get("cells", [])
        return " | ".join(_rich_text_plain(cell) for cell in cells if cell)

    rich_texts = type_data.get("rich_text", [])
    text = _rich_text_plain(rich_texts)
    if not text and "url" in type_data:
        caption = _rich_text_plain(type_data.get("caption", []))
        text = f"{caption}: {type_data['url']}" if caption else f"관련 링크: {type_data['url']}"
    return text


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


def _resolve_page_url(client: Client, page_id: str) -> Optional[str]:
    """하위 페이지의 공개 URL을 조회합니다 (공개 공유된 public_url 우선, 없으면 내부 url)."""
    try:
        page = client.pages.retrieve(page_id=page_id)
    except APIResponseError as e:
        logger.error("하위 페이지 URL 조회 실패 (id=%s): %s", page_id, e)
        return None
    return page.get("public_url") or page.get("url")


def _expand_blocks(
    client: Client, blocks: List[dict], depth: int = 0,
) -> Tuple[List[dict], List[Tuple[str, str]]]:
    """column_list/column/synced_block/callout을 펼쳐 청킹 로직이 보는 블록 목록을
    만들고, 그 안에서 발견되는 child_page는 (id, title) 목록으로 따로 모아 반환합니다.
    toggle/heading은 자신만 그대로 두고 자식은 펼치지 않습니다 — 그 자식들은 이미
    `_chunk_page_blocks`/`_chunk_by_headings`가 각자의 방식으로 따라가기 때문입니다.
    """
    expanded: List[dict] = []
    nested_pages: List[Tuple[str, str]] = []
    if depth > MAX_NESTING_DEPTH:
        return expanded, nested_pages

    for block in blocks:
        btype = block["type"]

        if btype == "child_page":
            nested_pages.append((block["id"], block.get("child_page", {}).get("title", "")))
            continue
        if btype == "child_database":
            logger.info("child_database는 현재 수집 대상이 아닙니다. 건너뜁니다 (id=%s).", block["id"])
            continue

        if btype not in TRANSPARENT_CONTAINER_TYPES:
            expanded.append(block)

        if block.get("has_children") and btype in (TRANSPARENT_CONTAINER_TYPES | TEXT_CONTAINER_TYPES):
            children = _fetch_all_blocks(client, block["id"])
            child_expanded, child_nested = _expand_blocks(client, children, depth + 1)
            expanded.extend(child_expanded)
            nested_pages.extend(child_nested)

    return expanded, nested_pages


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
        docs = _chunk_by_headings(client, non_toggle, source_origin, notion_page_url)
        documents.extend(docs)
    elif non_toggle:
        docs = _chunk_fallback(non_toggle, source_origin, notion_page_url)
        documents.extend(docs)

    return documents


def _chunk_by_headings(
    client: Client,
    blocks: List[dict],
    source_origin: str,
    notion_page_url: str,
) -> List[Document]:
    """heading_{1,2,3} 기준으로 블록을 묶어 Document 리스트 반환.

    Notion의 "토글 가능한 heading"은 본문이 형제 블록이 아니라 heading 자신의
    자식 블록으로 들어가므로, heading에 has_children이 있으면 자식을 재귀
    수집해 content_parts에 포함시킵니다(접지 않은 일반 heading의 형제 블록
    수집과 함께 동작).
    """
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
            if block.get("has_children"):
                children_text = _fetch_toggle_children_text(client, block["id"])
                if children_text:
                    content_parts.append(children_text)
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
        # 이미지/표처럼 텍스트를 전혀 추출할 수 없는 블록만 있는 페이지도, 페이지
        # 제목으로는 검색에서 찾아 노션 딥링크로 안내할 수 있어야 합니다. 본문이
        # 없다고 Document 자체를 만들지 않으면 그 페이지는 영구히 검색에서
        # 누락됩니다 (실제 발견된 사례: 본문이 일정표 이미지 1장뿐인 페이지).
        return [_make_document(source_origin, source_origin, "", notion_page_url, blocks[0]["id"])]

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
    _visited: Optional[set] = None,
) -> List[Document]:
    """
    단일 Notion 페이지를 수집해 Document 리스트를 반환합니다.
    page_id: UUID 형식 또는 하이픈 없는 32자 hex

    페이지 안에 column_list/callout 등으로 감싸인 하위 페이지(child_page)가 있으면
    각각의 공개 URL을 다시 조회해 재귀적으로 수집합니다 (_visited로 순환 참조 방지).
    """
    if _visited is None:
        _visited = set()
    if page_id in _visited:
        logger.warning("이미 수집한 페이지라 건너뜁니다 (순환 참조 방지): %s (id=%s)", page_name, page_id)
        return []
    _visited.add(page_id)

    logger.info("Notion 수집 시작: %s (id=%s)", page_name, page_id)
    blocks = _fetch_all_blocks(client, page_id)
    logger.info("  블록 수: %d", len(blocks))

    if not blocks:
        logger.warning("  '%s' 페이지가 비어있습니다.", page_name)
        return []

    expanded_blocks, nested_pages = _expand_blocks(client, blocks)
    docs = _chunk_page_blocks(client, expanded_blocks, page_name, notion_page_url)
    logger.info("  생성된 Document 수: %d", len(docs))

    for sub_id, sub_title in nested_pages:
        sub_url = _resolve_page_url(client, sub_id)
        if not sub_url:
            logger.warning("  하위 페이지 URL을 확인할 수 없어 건너뜁니다: %s (id=%s)", sub_title, sub_id)
            continue
        docs.extend(collect_notion_page(client, sub_id, sub_title or page_name, sub_url, _visited))

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
        page_name = page_name_map.get(key, key)

        if not url or "{{" in url:
            logger.warning("노션 페이지 '%s'의 URL이 설정되지 않았습니다. 건너뜁니다.", key)
            summary[key] = {"page_name": page_name, "skipped": True, "reason": "URL 미설정"}
            continue

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


def sync_notion_pages_incremental(config: dict, conn=None) -> Tuple[List[Document], dict]:
    """
    변경된 페이지만 재수집합니다 (cron 엔드포인트용).
    sync_metadata 테이블의 last_notion_edited_time과 비교합니다.

    최상위 페이지 자신의 수정 시각뿐 아니라, 재귀로 발견된 하위 페이지(child_page)
    각각의 수정 시각도 "{key}::{notion_page_id}" 키로 따로 추적합니다. 노션은 하위
    페이지만 수정해도 상위 페이지의 last_edited_time을 갱신하지 않으므로, 하위 페이지
    추적 없이는 그 변경이 영원히 감지되지 않습니다.

    Returns:
        (updated_docs, summary)
    """
    from datetime import datetime, timezone
    from storage.supabase_store import (
        get_sync_metadata, upsert_sync_metadata,
        get_sync_metadata_children, delete_sync_metadata_children,
    )

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
        page_name = page_name_map.get(key, key)

        if not url or "{{" in url:
            summary[key] = {"page_name": page_name, "skipped": True, "reason": "URL 미설정"}
            continue

        page_id = extract_page_id(url)

        # 마지막 수정 시각 확인 (최상위 페이지)
        last_edited = get_page_last_edited_time(client, page_id)
        if last_edited is None:
            summary[key] = {"page_name": page_name, "skipped": True, "reason": "페이지 조회 실패"}
            continue

        meta = get_sync_metadata(key, conn)
        top_changed = not (meta and meta["last_notion_edited_time"] == last_edited)

        # 최상위가 그대로면, 이전에 발견했던 하위 페이지들도 각자 변경됐는지 확인
        child_changed = False
        if not top_changed:
            for child_meta in get_sync_metadata_children(key, conn):
                child_id = child_meta["page_key"].split("::", 1)[1]
                child_last_edited = get_page_last_edited_time(client, child_id)
                if child_last_edited is None or child_last_edited != child_meta["last_notion_edited_time"]:
                    child_changed = True
                    break

        if not top_changed and not child_changed:
            logger.info("변경 없음, 스킵: %s (last_edited=%s)", page_name, last_edited)
            summary[key] = {
                "page_name": page_name,
                "doc_count": 0,
                "skipped": True,
                "reason": "변경 없음",
            }
            continue

        try:
            visited: set = set()
            docs = collect_notion_page(client, page_id, page_name, url, visited)
            all_docs.extend(docs)
            now = datetime.now(timezone.utc).isoformat()
            upsert_sync_metadata(key, last_edited, now, conn)

            # 이번에 실제로 방문한 하위 페이지 목록으로 교체합니다 (그 사이 추가/삭제된
            # 하위 페이지가 다음 비교에 정확히 반영되도록 기존 기록은 전부 지우고 새로 씀).
            delete_sync_metadata_children(key, conn)
            for sub_id in visited:
                if sub_id == page_id:
                    continue
                sub_last_edited = get_page_last_edited_time(client, sub_id)
                if sub_last_edited is not None:
                    upsert_sync_metadata(f"{key}::{sub_id}", sub_last_edited, now, conn)

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
